"""The guide's pilot, end to end against Postgres, with a fake model for the jokes and the judge."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest

from behaviors.judging.judge import Judgment
from behaviors.types import (
    AssistantMessage,
    ChatRollout,
    ContentText,
    ModelRequest,
    ModelResponse,
    StopEndTurn,
    Transcript,
    Usage,
    UserMessage,
)
from fxtr.project.client import ProjectClient
from fxtr.project.running import run_job
from joke_eval import experiment
from joke_eval.experiment import JokeReport

pytestmark = pytest.mark.anyio


class FakeModel:
    """A model that tells a joke about the topic it is asked about, or, as the judge, scores a
    coffee joke 8 and any other joke 4."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        prompt = "".join(
            block.text
            for message in request.context
            if isinstance(message, UserMessage)
            for block in message.content
            if isinstance(block, ContentText)
        )
        if self.model_name.endswith("opus-5-5"):
            score = 8 if "coffee" in prompt else 4
            text = json.dumps({"discussion": "A pun on the topic.", "score": score})
        else:
            text = f"{self.model_name} tells a joke: {prompt.split('"')[1]}"
        return ModelResponse(
            id="response",
            output=(AssistantMessage(content=(ContentText(text=text),)),),
            stop_reason=StopEndTurn(),
            usage=Usage(
                input_tokens=0,
                cache_read_input_tokens=None,
                cache_write_input_tokens=None,
                output_tokens=0,
                reasoning_output_tokens=None,
            ),
        )

    async def aclose(self) -> None:
        pass


async def test_the_pilot_tells_judges_and_summarizes_jokes(
    project_client: ProjectClient,
    launcher: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clients: list[FakeModel] = []

    def make_api(model_name: str) -> FakeModel:
        clients.append(FakeModel(model_name))
        return clients[-1]

    monkeypatch.setattr(experiment, "make_api", make_api)
    inputs = await launcher.build_inputs(project_client, "pilot")
    result = await run_job(project_client, experiment.evaluate, inputs, root=launcher.ROOT)

    report = await project_client.load(result.item(), as_type=JokeReport)
    jokes = await project_client.load(report.jokes)
    judgments = await project_client.load(report.judgments)
    topics = launcher.TOPICS[:3]
    expected = {(model, topic, 0) for model in launcher.MODELS for topic in topics}
    assert set(jokes.keys()) == set(judgments.keys()) == expected

    for key, reference in judgments.items():
        joke = ContentText(text=f"{launcher.MODELS[key[0]]} tells a joke: {key[1]}")
        rollout = await project_client.load(jokes[key], as_type=ChatRollout)
        assert rollout.model_turns[-1].messages[0].content == (joke,)
        judgment = await project_client.load(reference, as_type=Judgment)
        # The judgment refers to the transcript it judged, the joke's conversation, and was made
        # with the shared sampling.
        transcript = await project_client.load(
            judgment.entity_ids["transcript"], as_type=Transcript
        )
        assert transcript.messages[-1].content == (joke,)
        assert judgment.output == {
            "discussion": "A pun on the topic.",
            "score": 8 if key[1] == "coffee" else 4,
        }
        assert all(a.request.sampling == launcher.SAMPLING for a in judgment.attempts)

    # Each model was called by its full name, and the judge by the judge's.
    assert {client.model_name for client in clients} == {*launcher.MODELS.values(), launcher.JUDGE}
    summary = await project_client.load(report.summary)
    assert dict(summary.items()) == {
        (model,): {"jokes": 3, "graded": 3, "mean_score": (8 + 4 + 4) / 3}
        for model in launcher.MODELS
    }

    # The launcher prints the summary. Every step is cached, so launching again calls no model.
    calls = len(clients)
    await launcher.main("pilot")
    assert len(clients) == calls
    printed = capsys.readouterr().out
    for model in launcher.MODELS:
        assert f"{model}: mean score 5.33 (3 of 3 jokes graded)" in printed


async def test_a_model_name_needs_a_known_provider() -> None:
    with pytest.raises(ValueError, match="anthropic/MODEL or openai/MODEL"):
        experiment.make_api("claude-opus-5-5")


async def test_the_minimal_launcher_runs_one_joke(
    project_client: ProjectClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients: list[FakeModel] = []

    def make_api(model_name: str) -> FakeModel:
        clients.append(FakeModel(model_name))
        return clients[-1]

    @asynccontextmanager
    async def open_client(*args: object, **kwargs: object) -> AsyncIterator[ProjectClient]:
        yield project_client

    path = Path(__file__).parents[1] / "launch_minimal.py"
    spec = spec_from_file_location("joke_eval_minimal_launcher", path)
    assert spec is not None and spec.loader is not None
    minimal = module_from_spec(spec)
    spec.loader.exec_module(minimal)
    monkeypatch.setattr(minimal, "open_local_client", open_client)
    monkeypatch.setattr(experiment, "make_api", make_api)

    await minimal.main()
    assert [client.model_name for client in clients] == [
        "anthropic/claude-haiku-4-5-20251001",
        "anthropic/claude-opus-5-5",
    ]
