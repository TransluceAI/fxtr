"""Rollout projections preserve message order across stored rollout outcomes."""

import dataclasses

import pytest

from behaviors.interfaces import AgentRunProjection, TranscriptProjection
from behaviors.rollouts.chat import ChatRolloutTranscriptProjection
from behaviors.types import (
    AssistantMessage,
    ChatRollout,
    ContentText,
    ContextTurn,
    DynamicData,
    EndRollout,
    ModelCallFailure,
    ModelTurn,
    RolloutCompleted,
    RolloutFailed,
    SessionInitialization,
    SetSystemMessage,
    SystemMessage,
    UserMessage,
)
from fxtr.storage.memory_storage import MemoryStorage


@pytest.mark.anyio
@pytest.mark.parametrize("unanswered", [False, True])
@pytest.mark.parametrize("with_system", [False, True])
async def test_stored_rollout_projection_preserves_message_order(
    unanswered: bool, with_system: bool
) -> None:
    system = SystemMessage(content=(ContentText(text="system instructions"),))
    user = UserMessage(content=(ContentText(text="first question"),))
    assistant = AssistantMessage(content=(ContentText(text="first answer"),))
    final_user = UserMessage(content=(ContentText(text="second question"),))
    context_turns = (ContextTurn(messages=(user,), available_tools=None),)
    if unanswered:
        context_turns += (ContextTurn(messages=(final_user,), available_tools=None),)
    rollout = ChatRollout(
        context_turns=context_turns,
        model_turns=(ModelTurn(messages=(assistant,)),),
        status=(
            RolloutFailed(
                failure=ModelCallFailure(
                    category="context_length", description="full", provider_http_status=None
                )
            )
            if unanswered
            else RolloutCompleted()
        ),
        end=None if unanswered else EndRollout(),
        initialization=DynamicData.from_tagged(SetSystemMessage(system)) if with_system else None,
    )
    store = MemoryStorage()
    restored = await store.load(await store.store(rollout))
    projection = ChatRolloutTranscriptProjection()

    assert isinstance(projection, TranscriptProjection)
    assert isinstance(projection, AgentRunProjection)
    transcript = await projection.to_transcript(restored)
    assert transcript.messages == ((system,) if with_system else ()) + (
        (user, assistant, final_user) if unanswered else (user, assistant)
    )
    run = await projection.to_agent_run(restored)
    assert run.transcripts == (transcript,)
    assert run.groups == ()


@pytest.mark.anyio
async def test_empty_rollout_projection() -> None:
    projection = ChatRolloutTranscriptProjection()
    result = await projection.to_transcript(
        ChatRollout(context_turns=(), model_turns=(), status=RolloutCompleted(), end=EndRollout())
    )
    assert result.messages == ()


@dataclasses.dataclass(frozen=True)
class SceneSetup(SessionInitialization, fxtr_type="test.behaviors.SceneSetup.v1"):
    scene: str


@pytest.mark.anyio
async def test_projection_omits_other_initialization_types() -> None:
    projection = ChatRolloutTranscriptProjection()
    result = await projection.to_transcript(
        ChatRollout(
            context_turns=(),
            model_turns=(),
            status=RolloutCompleted(),
            end=EndRollout(),
            initialization=DynamicData.from_tagged(SceneSetup("a quiet library")),
        )
    )
    assert result.messages == ()
