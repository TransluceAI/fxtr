"""Behaviors rollouts cross the fxtr step, workflow, and storage boundaries."""

from dataclasses import replace

import pytest

from behavior_rollouts import openai_rollout
from behavior_rollouts.scripted_rollout import EchoAPI, scripted_experiment
from behaviors.types import (
    ChatRollout,
    ContentText,
    ModelRequest,
    ModelResponse,
    RolloutCompleted,
    find_metadata,
)
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.project.client import ProjectClient
from fxtr.project.running import run_job

pytestmark = pytest.mark.anyio


async def test_openai_models_and_sequences_map_to_isolated_ordered_conversations(
    monkeypatch: pytest.MonkeyPatch,
    project_client: ProjectClient,
) -> None:
    class RecordingAPI(EchoAPI):
        def __init__(self, model_name: str) -> None:
            self.model_name = model_name
            self.requests: list[ModelRequest] = []
            self.closed = False
            clients.append(self)

        async def generate(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            response = await super().generate(request)
            return replace(
                response,
                output=tuple(
                    replace(
                        message,
                        content=tuple(
                            ContentText(text=f"{self.model_name}: {block.text}")
                            for block in message.content
                            if isinstance(block, ContentText)
                        ),
                    )
                    for message in response.output
                ),
            )

        async def aclose(self) -> None:
            self.closed = True

    clients: list[RecordingAPI] = []
    monkeypatch.setattr(openai_rollout, "OpenAIResponsesAPI", RecordingAPI)
    sequences = openai_rollout.DEFAULT_QUESTION_SEQUENCES
    models = ["test-small", "test-large"]
    assert len(sequences) == 10
    assert all(len(questions) == 5 for questions in sequences.values())
    result = await run_job(
        project_client,
        openai_rollout.openai_experiment,
        inputs={
            "questions": openai_rollout.question_sequences_array(sequences),
            "model_name": openai_rollout.models_array(models),
            "max_tokens": Array.scalar(128, int),
        },
    )
    assert result.type == ArrayType.of(("[model: str, sequence: str]", BoundID[ChatRollout]))
    assert set(result.keys()) == {(model, name) for model in models for name in sequences}
    assert len(await project_client.list_cache_entries()) == len(clients) == 20
    for (model, name), reference in result.items():
        assert isinstance(name, str)
        rollout = await project_client.load(reference, as_type=ChatRollout)
        assert isinstance(rollout.status, RolloutCompleted)
        assert rollout.end is not None
        assert find_metadata(rollout.end.metadata, openai_rollout.QuestionsAsked) == (
            openai_rollout.QuestionsAsked(questions_asked=5)
        )
        assert [
            find_metadata(turn.metadata, openai_rollout.QuestionIndex)
            for turn in rollout.context_turns
        ] == [openai_rollout.QuestionIndex(question_index=index) for index in range(5)]
        assert [turn.messages[0].content for turn in rollout.model_turns] == [
            (ContentText(text=f"{model}: Echo: {question}"),) for question in sequences[name]
        ]
    for client in clients:
        assert client.closed
        assert len(client.requests) == 5
        # Each client starts with its own system/user pair and adds two messages each round.
        assert [len(request.context) for request in client.requests] == [2, 4, 6, 8, 10]
        assert all(request.sampling.max_tokens == 128 for request in client.requests)
        for request in client.requests:
            for message in request.context:
                if message.role == "assistant":
                    assert all(
                        isinstance(block, ContentText)
                        and block.text.startswith(f"{client.model_name}: Echo:")
                        for block in message.content
                    )
    assert await openai_rollout.report_results(project_client, result) == 0


@pytest.mark.parametrize("sequences", [{}, {"empty": []}, {"blank": [" "]}, {"": ["Hello"]}])
async def test_question_sequences_reject_empty_data(sequences: dict[str, list[str]]) -> None:
    with pytest.raises(ValueError, match="nonempty"):
        openai_rollout.question_sequences_array(sequences)


@pytest.mark.parametrize("models", [[], [""], [" "], ["same", "same"]])
async def test_models_reject_empty_or_duplicate_names(models: list[str]) -> None:
    with pytest.raises(ValueError, match="unique, nonempty"):
        openai_rollout.models_array(models)


async def test_mapped_rollouts_are_stored_and_loadable_from_workflow_results(
    project_client: ProjectClient,
) -> None:
    prompts = {"greeting": "Hello.", "question": "What is a prime number?"}
    result = await run_job(
        project_client,
        scripted_experiment,
        inputs={
            "prompts": Array.infer_from_items(
                [((name,), prompt) for name, prompt in prompts.items()], dims=["prompt"]
            )
        },
    )
    assert result.type == ArrayType.of(("[prompt: str]", BoundID[ChatRollout]))
    assert set(result.keys()) == {(name,) for name in prompts}
    assert len(await project_client.list_cache_entries()) == 2
    for (name,), reference in result.items():
        assert isinstance(name, str)
        rollout = await project_client.load(reference, as_type=ChatRollout)
        assert isinstance(rollout.status, RolloutCompleted)
        assert rollout.end is not None
        assert len(rollout.context_turns) == len(rollout.model_turns) == 2
        assert rollout.model_turns[0].messages[0].content == (
            ContentText(text=f"Echo: {prompts[name]}"),
        )
        assert rollout.model_turns[1].messages[0].content == (ContentText(text="Echo: Thank you."),)
        response = find_metadata(rollout.model_turns[1].metadata, ModelResponse)
        assert response is not None
        assert response.id == "echo-3"
