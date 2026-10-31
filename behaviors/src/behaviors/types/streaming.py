"""Events emitted while streaming a single model response.

Delta events are passed to a callback while the fully assembled response is
returned by the generating call itself (see
``behaviors.interfaces.model_apis.StreamingModelAPI`` and
``behaviors.interfaces.chat_session_models.StreamingChatSession``). Consumers
of these events never assemble content: they relay progress to UIs and proxies.
A stream carries no terminal event; the return of the generating call is the end.

The delta types match the content-block types in ``behaviors.types.content``
one-to-one (text, raw reasoning, summarized reasoning and its signature, tool
use), so there is a single vocabulary rather than two related ones; redacted
reasoning has no delta type because providers deliver it whole.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class StreamTextDelta(TaggedDataclass, fxtr_type=behaviors_type("StreamTextDelta.v1")):
    """A fragment of assistant text (see ContentText).

    Attributes:
        text: The text fragment.
    """

    text: str
    kind: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True)
class StreamReasoningDelta(TaggedDataclass, fxtr_type=behaviors_type("StreamReasoningDelta.v1")):
    """A fragment of a literal reasoning trace (see ContentReasoning).

    Attributes:
        text: The reasoning text fragment.
    """

    text: str
    kind: Literal["reasoning_delta"] = "reasoning_delta"


@dataclass(frozen=True)
class StreamSummarizedReasoningDelta(
    TaggedDataclass, fxtr_type=behaviors_type("StreamSummarizedReasoningDelta.v1")
):
    """A fragment of a summarized reasoning trace (see ContentSummarizedReasoning).

    Attributes:
        text: The summarized reasoning text fragment.
    """

    text: str
    kind: Literal["summarized_reasoning_delta"] = "summarized_reasoning_delta"


@dataclass(frozen=True)
class StreamSignatureDelta(TaggedDataclass, fxtr_type=behaviors_type("StreamSignatureDelta.v1")):
    """A fragment of a reasoning block's provider signature.

    Corresponds to ContentSummarizedReasoning's `signature`; providers stream
    it in fragments at the end of the reasoning block (Anthropic
    `signature_delta`).

    Attributes:
        signature: The signature fragment.
    """

    signature: str
    kind: Literal["signature_delta"] = "signature_delta"


@dataclass(frozen=True)
class StreamToolUseStart(TaggedDataclass, fxtr_type=behaviors_type("StreamToolUseStart.v1")):
    """The model started a tool call (see ContentToolUse).

    Attributes:
        index: Position of this tool call among the response's tool calls,
            used to correlate subsequent input deltas.
        id: Provider-assigned tool-call ID (see ContentToolUse.id).
        name: Name of the tool being invoked.
    """

    index: int
    id: str
    name: str
    kind: Literal["tool_use_start"] = "tool_use_start"


@dataclass(frozen=True)
class StreamToolUseInputDelta(
    TaggedDataclass, fxtr_type=behaviors_type("StreamToolUseInputDelta.v1")
):
    """A fragment of a tool call's JSON-encoded input.

    Attributes:
        index: Which tool call this fragment belongs to (see
            StreamToolUseStart.index).
        partial_json: The raw JSON fragment, concatenable with the other
            fragments for this index.
    """

    index: int
    partial_json: str
    kind: Literal["tool_use_input_delta"] = "tool_use_input_delta"


@dataclass(frozen=True)
class StreamRestart(TaggedDataclass, fxtr_type=behaviors_type("StreamRestart.v1")):
    """A partially streamed attempt was abandoned and generation restarted.

    Emitted when an implementation retries after already having emitted
    events for this response (e.g. the provider connection dropped
    mid-stream). Consumers must discard every event received before this one;
    none of it will appear in the final response.
    """

    kind: Literal["restart"] = "restart"


StreamEvent = (
    StreamTextDelta
    | StreamReasoningDelta
    | StreamSummarizedReasoningDelta
    | StreamSignatureDelta
    | StreamToolUseStart
    | StreamToolUseInputDelta
    | StreamRestart
)

type StreamEventCallback = Callable[[StreamEvent], Awaitable[None]]
"""Receives each stream event as it arrives."""
