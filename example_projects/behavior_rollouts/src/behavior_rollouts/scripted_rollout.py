"""Run a scripted conversation inside a fxtr workflow without calling a model provider.

Run ``uv run python launch_scripted.py`` from the project directory. Substitute a provider
ModelAPI for EchoAPI to use the same rollout path with a real model.

Each conversation's longest reply is then picked out (:class:`LongestReply`), with a
:class:`MessageSelector` saying which message it is. The project's views link from it to the
conversation with the selector, which the conversation view marks: the selector's shape here and
the view's (``fxtr-view-behaviors``' ``MessageSelector``) are kept the same by hand.
"""

import dataclasses
from typing import Annotated, cast

from behaviors.implementations.context_policies import (
    ScriptedUserSimulator,
    context_policy_from_user_simulator,
)
from behaviors.implementations.models import chat_session_model_from_api
from behaviors.rollouts.chat import run_chat_rollout
from behaviors.types import (
    AssistantMessage,
    ChatMessage,
    ChatRollout,
    ContentText,
    ModelRequest,
    ModelResponse,
    SamplingParams,
    StopEndTurn,
    Usage,
    UserMessage,
)
from fxtr.core.entities import BoundID
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.experiment.handles import ArrayHandle
from fxtr.experiment.steps import StepContext, step
from fxtr.experiment.workflows import WorkflowContext, workflow


class EchoAPI:
    """A deterministic ModelAPI that echoes the latest user message."""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        user = next(
            message for message in reversed(request.context) if isinstance(message, UserMessage)
        )
        text = "".join(block.text for block in user.content)
        return ModelResponse(
            id=f"echo-{len(request.context)}",
            output=(AssistantMessage(content=(ContentText(text=f"Echo: {text}"),)),),
            stop_reason=StopEndTurn(),
            usage=Usage(
                input_tokens=0,
                cache_read_input_tokens=None,
                cache_write_input_tokens=None,
                output_tokens=0,
                reasoning_output_tokens=None,
            ),
        )


@step(name="scripted_rollout.answer_prompt")
async def answer_prompt(context: StepContext, prompt: str) -> ChatRollout:
    """Roll out two scripted user turns; fxtr stores the returned ChatRollout."""
    model = chat_session_model_from_api(EchoAPI(), SamplingParams(max_tokens=100))
    user = ScriptedUserSimulator(
        messages=(
            UserMessage(content=(ContentText(text=prompt),)),
            UserMessage(content=(ContentText(text="Thank you."),)),
        )
    )
    policy = context_policy_from_user_simulator(user, initialization=None)
    return await run_chat_rollout(model, policy, max_turns=5)


@dataclasses.dataclass(frozen=True)
class MessageSelector:
    """One message of a conversation: its index among the conversation's messages, each turn's
    context messages and then its model's, turn by turn (:func:`conversation_messages`).

    Stored untagged (``{"message": 3}``), the shape the behaviors conversation view reads as its
    ``selector`` param.
    """

    message: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class LongestReply(
    DataclassEntity, fxtr_type="org.transluce.examples.behavior_rollouts.LongestReply.v1"
):
    """The longest of a conversation's replies, and where it is in the conversation."""

    rollout: BoundID[ChatRollout]
    selector: MessageSelector
    text: str


def conversation_messages(rollout: ChatRollout) -> list[ChatMessage]:
    """A rollout's messages in the order its conversation view draws them."""
    messages: list[ChatMessage] = []
    for index in range(max(len(rollout.context_turns), len(rollout.model_turns))):
        if index < len(rollout.context_turns):
            messages.extend(rollout.context_turns[index].messages)
        if index < len(rollout.model_turns):
            messages.extend(rollout.model_turns[index].messages)
    return messages


def message_text(message: ChatMessage) -> str:
    return "".join(block.text for block in message.content if isinstance(block, ContentText))


def longest_reply(rollout: ChatRollout) -> tuple[MessageSelector, str]:
    """The conversation's longest assistant message (the first, of several as long): where it is,
    and its text."""
    replies = [
        (index, message_text(message))
        for index, message in enumerate(conversation_messages(rollout))
        if isinstance(message, AssistantMessage)
    ]
    if not replies:
        raise ValueError("the conversation has no replies")
    index, text = max(replies, key=lambda reply: len(reply[1]))
    return MessageSelector(message=index), text


@step(name="scripted_rollout.pick_longest_reply")
async def pick_longest_reply(context: StepContext, rollout: BoundID[ChatRollout]) -> LongestReply:
    """The conversation's longest reply, with a selector into the conversation for it."""
    selector, text = longest_reply(await context.load(rollout))
    return LongestReply(rollout=rollout, selector=selector, text=text)


@workflow(name="scripted_rollout.scripted_experiment")
async def scripted_experiment(
    context: WorkflowContext,
    prompts: Annotated[ArrayHandle[str], "[prompt: str]"],
) -> Annotated[ArrayHandle[BoundID[ChatRollout]], "[prompt: str]"]:
    """Map a behaviors rollout over prompts using the fxtr runner, and pick each one's longest
    reply."""
    rollouts = context.run_step(
        "answer", answer_prompt, {"prompt": prompts}, map_over=["prompt"]
    ).expect_type(("[prompt: str]", BoundID[ChatRollout]))
    context.run_step("longest", pick_longest_reply, {"rollout": rollouts}, map_over=["prompt"])
    # Entity-returning steps store their results as references.
    return cast(ArrayHandle[BoundID[ChatRollout]], rollouts)
