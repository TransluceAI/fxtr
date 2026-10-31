"""Helpers shared by the context policy implementations."""

import dataclasses
from typing import Literal

from behaviors.types.content import Content, ContentText, ContentToolUse
from behaviors.types.message import AssistantMessage, ChatMessage, ToolMessage
from behaviors.types.namespace import behaviors_type
from behaviors.types.rollout import ModelTurn
from fxtr.entity_defns.dataclass_bases import TaggedDataclass

END_TRANSCRIPT_MARKER = "</transcript>"
"""What a simulated user's raw text ends with when the user stops chatting."""


@dataclasses.dataclass(frozen=True)
class SimulatedUserText(TaggedDataclass, fxtr_type=behaviors_type("SimulatedUserText.v1")):
    """Turn (or end-of-rollout) metadata: the text a simulated user produced, as produced.

    A pilot steering the simulator is shown candidates in this form.

    Attributes:
        source: ``"sampled"`` from the simulator's model, ``"fixed"`` by its configuration
            (e.g. a scripted opening message), or a caller's ``"override"``.
        raw_text: The text before it became the turn's message, so it may still carry
            surrounding whitespace or, for a turn that ended the rollout,
            ``END_TRANSCRIPT_MARKER``.
    """

    source: Literal["sampled", "fixed", "override"]
    raw_text: str


def tool_uses_in(model_turn: ModelTurn) -> tuple[ContentToolUse, ...]:
    """The tool-use blocks in a model turn, in order."""
    return tuple(
        block
        for message in model_turn.messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, ContentToolUse)
    )


def strip_tool_uses(messages: tuple[ChatMessage, ...]) -> tuple[ChatMessage, ...]:
    """Remove tool-use blocks and tool messages, dropping messages left empty."""
    stripped: list[ChatMessage] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            content: tuple[Content, ...] = tuple(
                block for block in message.content if not isinstance(block, ContentToolUse)
            )
            if not content:
                continue
            stripped.append(AssistantMessage(content=content))
        elif isinstance(message, ToolMessage):
            continue
        else:
            stripped.append(message)
    return tuple(stripped)


def message_text(message: ChatMessage) -> str:
    """The text of a message as a simulated user reads it.

    Reasoning blocks are skipped. Tool calls and tool results have no place in a
    text-only transcript and raise ``ValueError``, as does a message left with no text.
    """
    if isinstance(message, ToolMessage):
        raise ValueError("tool messages have no text-only rendering")
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, ContentText):
            parts.append(block.text)
        elif isinstance(block, ContentToolUse):
            raise ValueError("tool calls have no text-only rendering")
    text = "".join(parts).strip()
    if not text:
        raise ValueError(f"message has no text: {message!r}")
    return text


def ends_transcript(raw_text: str) -> bool:
    """Whether a simulated user's raw text ends the conversation.

    Any occurrence of ``END_TRANSCRIPT_MARKER`` does, and nothing around the marker is sent.
    """
    return END_TRANSCRIPT_MARKER in raw_text
