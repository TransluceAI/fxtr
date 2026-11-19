"""Behaviors rollouts cross the fxtr step, workflow, and storage boundaries."""

import pytest

from behavior_rollouts.echo import (
    MessageSelector,
    experiment,
    longest_reply,
)
from behaviors.types import (
    ChatRollout,
    ContentText,
    ModelResponse,
    RolloutCompleted,
    find_metadata,
)
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.project.client import ProjectClient
from fxtr.project.running import run_job

pytestmark = pytest.mark.anyio


async def test_mapped_rollouts_are_stored_and_loadable_from_workflow_results(
    project_client: ProjectClient,
) -> None:
    prompts = {"greeting": "Hello.", "question": "What is a prime number?"}
    result = await run_job(
        project_client,
        experiment,
        inputs={
            "prompts": Array.infer_from_items(
                [((name,), prompt) for name, prompt in prompts.items()], dims=["prompt"]
            )
        },
    )
    assert result.type == ArrayType.of(("[prompt: str]", BoundID[ChatRollout]))
    assert set(result.keys()) == {(name,) for name in prompts}
    # A rollout and its longest reply, for each prompt.
    assert len(await project_client.list_cache_entries()) == 4
    assert len(await project_client.list_cache_entries(step="echo.pick_longest_reply")) == 2
    # The echo of "Thank you." outgrows that of "Hello.", not of the question: the fourth message
    # of the conversation, or the second.
    longest = {"greeting": 3, "question": 1}
    for (name,), reference in result.items():
        assert isinstance(name, str)
        rollout = await project_client.load(reference, as_type=ChatRollout)
        assert isinstance(rollout.status, RolloutCompleted)
        assert rollout.end is not None
        assert len(rollout.context_turns) == len(rollout.model_turns) == 2
        assert rollout.model_turns[0].messages[0].content == (
            ContentText(text=f"Echo: {prompts[name]}"),
        )
        assert longest_reply(rollout)[0] == MessageSelector(message=longest[name])
        assert rollout.model_turns[1].messages[0].content == (ContentText(text="Echo: Thank you."),)
        response = find_metadata(rollout.model_turns[1].metadata, ModelResponse)
        assert response is not None
        assert response.id == "echo-3"
