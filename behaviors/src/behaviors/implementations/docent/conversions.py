"""Conversions from behaviors types to the public Docent SDK types.

Message conversion is lossy where Docent's model is narrower: system/user/tool
message content flattens to text, and redacted reasoning keeps only the fact
that reasoning was redacted (the encrypted payload has no Docent
representation). Tool-use content blocks become the assistant message's
``tool_calls``.
"""

import uuid
from collections.abc import Callable

from docent.data_models import AgentRun as DocentAgentRun
from docent.data_models import Transcript as DocentTranscript
from docent.data_models import TranscriptGroup as DocentTranscriptGroup
from docent.data_models.chat import AssistantMessage as DocentAssistantMessage
from docent.data_models.chat import ChatMessage as DocentChatMessage
from docent.data_models.chat import SystemMessage as DocentSystemMessage
from docent.data_models.chat import ToolCall as DocentToolCall
from docent.data_models.chat import ToolMessage as DocentToolMessage
from docent.data_models.chat import UserMessage as DocentUserMessage
from docent.data_models.chat.content import ContentReasoning as DocentContentReasoning
from docent.data_models.chat.content import ContentText as DocentContentText

from behaviors.types.agent_run import AgentRun, Transcript, TranscriptGroup
from behaviors.types.content import (
    Content,
    ContentReasoning,
    ContentRedactedReasoning,
    ContentSummarizedReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.message import (
    AssistantMessage,
    ChatMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)


def _text_of(content: tuple[Content, ...]) -> str:
    return "\n\n".join(block.text for block in content if isinstance(block, ContentText))


def to_docent_message(message: ChatMessage) -> DocentChatMessage:
    """Convert a behaviors ChatMessage to a Docent chat message."""
    if isinstance(message, SystemMessage):
        return DocentSystemMessage(content=_text_of(message.content))
    if isinstance(message, UserMessage):
        return DocentUserMessage(content=_text_of(message.content))
    if isinstance(message, AssistantMessage):
        content: list[DocentContentText | DocentContentReasoning] = []
        tool_calls: list[DocentToolCall] = []
        for block in message.content:
            if isinstance(block, ContentText):
                content.append(DocentContentText(text=block.text))
            elif isinstance(block, ContentReasoning):
                content.append(DocentContentReasoning(reasoning=block.text))
            elif isinstance(block, ContentSummarizedReasoning):
                content.append(
                    DocentContentReasoning(
                        reasoning="", summary=block.text, signature=block.signature
                    )
                )
            elif isinstance(block, ContentRedactedReasoning):
                content.append(DocentContentReasoning(reasoning="", redacted=True))
            elif isinstance(block, ContentToolUse):  # pyright: ignore[reportUnnecessaryIsInstance]
                tool_calls.append(
                    DocentToolCall(
                        id=block.id,
                        function=block.name,
                        arguments=dict(block.input),
                        parse_error=block.parse_error,
                    )
                )
        return DocentAssistantMessage(content=content, tool_calls=tool_calls or None)
    if isinstance(message, ToolMessage):  # pyright: ignore[reportUnnecessaryIsInstance]
        return DocentToolMessage(
            content=_text_of(message.content),
            tool_call_id=message.tool_call_id,
            error={"is_error": True} if message.is_error else None,
        )
    raise ValueError(f"Unsupported message type: {type(message)}")


def to_docent_transcript(
    transcript: Transcript,
    *,
    name_override: str | None = None,
    metadata_override: dict[str, object] | None = None,
    to_message_override: Callable[[ChatMessage], DocentChatMessage] | None = None,
    transcript_group_id: str | None = None,
) -> DocentTranscript:
    """Convert a behaviors Transcript to a Docent Transcript.

    Args:
        transcript: The transcript to convert.
        name_override: Optional name to use instead of the transcript's name.
        metadata_override: Optional metadata merged over the transcript's own.
        to_message_override: Optional replacement for ``to_docent_message``.
        transcript_group_id: Optional Docent transcript group this transcript
            belongs to.
    """
    to_message = to_message_override or to_docent_message
    metadata: dict[str, object] = dict(transcript.metadata)
    if metadata_override:
        metadata = {**metadata, **metadata_override}
    return DocentTranscript(
        name=name_override or transcript.name,
        description=transcript.description,
        messages=[to_message(message) for message in transcript.messages],
        metadata=metadata,
        transcript_group_id=transcript_group_id,
    )


def to_docent_agent_run(
    run: AgentRun,
    *,
    name_override: str | None = None,
    metadata_override: dict[str, object] | None = None,
) -> DocentAgentRun:
    """Convert a behaviors AgentRun to a Docent AgentRun.

    Nested transcript groups become Docent transcript groups linked by
    generated ids; each transcript keeps its place in the hierarchy via
    ``transcript_group_id``.
    """
    run_id = str(uuid.uuid4())
    transcripts = [to_docent_transcript(t) for t in run.transcripts]
    groups: list[DocentTranscriptGroup] = []

    def walk(group: TranscriptGroup, parent_id: str | None) -> None:
        group_id = str(uuid.uuid4())
        groups.append(
            DocentTranscriptGroup(
                id=group_id,
                name=group.name,
                description=group.description,
                agent_run_id=run_id,
                parent_transcript_group_id=parent_id,
                metadata=dict(group.metadata),
            )
        )
        transcripts.extend(
            to_docent_transcript(t, transcript_group_id=group_id) for t in group.transcripts
        )
        for child in group.groups:
            walk(child, group_id)

    for group in run.groups:
        walk(group, None)

    metadata: dict[str, object] = dict(run.metadata)
    if metadata_override:
        metadata = {**metadata, **metadata_override}
    return DocentAgentRun(
        id=run_id,
        name=name_override or run.name,
        description=run.description,
        transcripts=transcripts,
        transcript_groups=groups,
        metadata=metadata,
    )
