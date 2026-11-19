"""Matched case isolation and the belief-reinforcement workflow's stored judgments."""

import json
from dataclasses import replace

import pytest

from behavior_rollouts import belief_reinforcement, transcript_judging
from behavior_rollouts.belief_reinforcement_cases import (
    SITUATIONS,
    BeliefReinforcementCase,
    matched_cases,
)
from behavior_rollouts.echo import EchoAPI
from behavior_rollouts.models import split_provider, store_model_specs
from behavior_rollouts.transcript_judging import JUDGE_INSTRUCTIONS
from behaviors.judging.judge import Judgment
from behaviors.types import (
    AssistantMessage,
    ContentText,
    ModelRequest,
    ModelResponse,
    ReasoningParams,
    TextEntity,
)
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array
from fxtr.project.client import ProjectClient
from fxtr.project.running import run_job


def test_matched_cases_vary_only_the_evidence_turn() -> None:
    cases = matched_cases(list(SITUATIONS), ["weak", "mixed", "strong"])
    assert len(cases) == 15
    for name in SITUATIONS:
        variants = [case for case in cases.values() if case["situation"] == name]
        assert {case["evidence"] for case in variants} == {"weak", "mixed", "strong"}
        assert len({case["turns"][1] for case in variants}) == 3
        assert all(len(case["turns"]) == 4 for case in variants)
        for index in (0, 2, 3):
            assert len({case["turns"][index] for case in variants}) == 1


class FakeModel(EchoAPI):
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = await super().generate(request)
        if not self.model_name.endswith("judge"):
            return response
        prompt = " ".join(
            block.text
            for message in request.context
            for block in message.content
            if isinstance(block, ContentText)
        )
        assert "friends_weak" not in prompt
        assert "friends_strong" not in prompt
        match = "two of them having lunch" in prompt
        answer = json.dumps(
            {
                "discussion": (
                    "The user gives their account [transcript:B1L0]. "
                    "The assistant echoes it [transcript:B2L0]."
                ),
                "match": match,
                "emotional_responsiveness": "mixed",
                "overall": "poor" if match else "good",
                "first_reinforcement": "[transcript:B2L0]" if match else "",
            }
        )
        return replace(response, output=(AssistantMessage(content=(ContentText(text=answer),)),))

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_workflow_records_conditions_and_all_judgment_fields(
    project_client: ProjectClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: list[FakeModel] = []

    def make_api(model_name: str) -> FakeModel:
        split_provider(model_name)
        api = FakeModel(model_name)
        clients.append(api)
        return api

    monkeypatch.setattr(belief_reinforcement, "make_api", make_api)
    monkeypatch.setattr(transcript_judging, "make_api", make_api)
    cases = matched_cases(["friends"], ["weak", "mixed", "strong"])
    model_name, sampling = await store_model_specs(
        project_client, ["subject-a", "subject-b"], dim="model", max_tokens=128
    )
    judge_model_name, judge_sampling = await store_model_specs(
        project_client, ["openai/judge", "anthropic/judge:low"], dim="judge", max_tokens=128
    )
    result = await run_job(
        project_client,
        belief_reinforcement.experiment,
        {
            "cases": Array.from_items(
                [((name,), case) for name, case in cases.items()],
                type=("[case: str]", BeliefReinforcementCase),
            ),
            "model_name": model_name,
            "sampling": sampling,
            "judge_model_name": judge_model_name,
            "judge_sampling": judge_sampling,
            "system_prompt": Array.scalar(belief_reinforcement.SYSTEM_PROMPT, str),
            "judge_instructions": Array.scalar(
                await project_client.store(JUDGE_INSTRUCTIONS), BoundID[TextEntity]
            ),
            "rubric": Array.scalar(
                await project_client.store(belief_reinforcement.RUBRIC), BoundID[TextEntity]
            ),
            "output_schema": Array.scalar(
                await project_client.store(
                    TextEntity(text=json.dumps(belief_reinforcement.OUTPUT_SCHEMA))
                ),
                BoundID[TextEntity],
            ),
            "judge_samples": Array.scalar(2, int),
        },
    )
    report = await project_client.load(
        result.item(), as_type=belief_reinforcement.BeliefReinforcementReport
    )
    stored_cases = await project_client.load(report.cases)
    assert dict(stored_cases.items()) == {(name,): case for name, case in cases.items()}
    rollouts = await project_client.load(report.rollouts)
    judgments = await project_client.load(report.judgments)
    verdicts = await project_client.load(report.verdicts)
    assert len(list(rollouts.items())) == 6
    assert len(list(judgments.items())) == len(list(verdicts.items())) == 24
    for key, ref in judgments.items():
        judgment = await project_client.load(ref, as_type=Judgment)
        assert judgment.output is not None
        match = key[1] == "friends_weak"
        assert verdicts[key] == {
            "reinforcement": match,
            "emotional_responsiveness": "mixed",
            "overall": "poor" if match else "good",
            "answers": 1,
        }
        assert (
            any(c.path_to_output_field == ("first_reinforcement",) for c in judgment.citations)
            == match
        )
    # Each model was built from its name, provider prefix and all, and sampled as its spec says.
    assert {client.model_name for client in clients} == {
        "subject-a",
        "subject-b",
        "openai/judge",
        "anthropic/judge",
    }
    efforts = {
        client.model_name: {r.sampling.reasoning for r in client.requests} for client in clients
    }
    assert efforts["anthropic/judge"] == {ReasoningParams(effort="low")}
    assert efforts["openai/judge"] == efforts["subject-a"] == {None}
    assert all(client.closed for client in clients)
    assert await belief_reinforcement.report_results(project_client, result) == 0
