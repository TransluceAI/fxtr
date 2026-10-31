"""Runs the example project's array queries experiment on Postgres."""

from __future__ import annotations

import pytest
from reference_backends import Execution, Executor

from basic_project.array_queries import Transcript, evaluate_models, report, solve

pytestmark = pytest.mark.anyio


async def run(execute: Executor) -> Execution:
    return await execute(evaluate_models, solve, report)


async def test_the_report_answers_every_question_from_queries(execute: Executor) -> None:
    record = await run(execute)
    # Stored values are raw, so the report's lists come back as tuples.
    assert record.result.item() == {
        # large and xl both solved 4 of 5; large did so for less.
        "most_accurate": ("large", "xl"),
        "best_value": "large",
        # 1170 tokens at $0.1, 890 at $0.4, 800 at $2, and 800 at $5 per thousand.
        "total_cost": pytest.approx(0.117 + 0.356 + 1.6 + 4.0),
        "unsolved": ("riemann",),
        "failures": 4,
        "cheapest_solvers": (
            "primes: small ($0.040)",
            "proof: large ($0.300)",
            "sum: tiny ($0.005)",
            "triangle: small ($0.040)",
        ),
    }


async def test_triage_copies_transcript_references_through_its_query(execute: Executor) -> None:
    record = await run(execute)
    # The report step received the triage query's result as its `failures` input.
    failures = await record.storage.load(record.steps["root/report"].inputs["failures"])
    assert sorted(failures.keys()) == [
        ("large", "riemann"),
        ("small", "riemann"),
        ("tiny", "proof"),
        ("tiny", "riemann"),
    ]
    for (model, _), failure in failures.items():
        assert failure["error"] == "ran out of tokens"
        transcript = await record.storage.load(failure["transcript"], as_type=Transcript)
        assert transcript.model == model
