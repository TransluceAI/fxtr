"""Events emitted while streaming a single model response.

Delta events are passed to a callback while the fully assembled response is
returned by the generating call itself (see
``behaviors.interfaces.model_apis.StreamingModelAPI`` and
``behaviors.interfaces.chat_session_models.StreamingChatSession``). Consumers
of these events never assemble content: they relay progress to UIs and proxies.
A stream carries no terminal event; the return of the generating call is the end.

The events follow the content-block types in ``behaviors.types.content``, so
there is a single vocabulary rather than two related ones: text deltas; a
reasoning block as its start (with its form), its text deltas and, once whole,
its provider state; a tool call as its start and its input deltas.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from behaviors.types.content import ReasoningForm
from behaviors.types.failure import ModelCallFailure
from behaviors.types.namespace import behaviors_type
from behaviors.types.response import ModelResponse
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData


@dataclass(frozen=True)
class StreamTextDelta(TaggedDataclass, fxtr_type=behaviors_type("StreamTextDelta.v1")):
    """A fragment of assistant text (see ContentText).

    Attributes:
        text: The text fragment.
    """

    text: str
    kind: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True)
class StreamReasoningStart(TaggedDataclass, fxtr_type=behaviors_type("StreamReasoningStart.v1")):
    """The start of a reasoning block (see ContentReasoning).

    The block runs until the next start event, text delta or tool-use start, or the end of the
    stream; its text arrives in `StreamReasoningDelta`s and its state, if any, in one
    `StreamReasoningState`.

    Attributes:
        form: What the block's text is.
    """

    form: ReasoningForm
    kind: Literal["reasoning_start"] = "reasoning_start"


@dataclass(frozen=True)
class StreamReasoningDelta(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("StreamReasoningDelta.v2"),
):
    """A fragment of the open reasoning block's text.

    Attributes:
        text: The reasoning text fragment.
    """

    text: str
    kind: Literal["reasoning_delta"] = "reasoning_delta"


@dataclass(frozen=True)
class StreamReasoningState(TaggedDataclass, fxtr_type=behaviors_type("StreamReasoningState.v1")):
    """The open reasoning block's provider state, whole (see `ContentReasoning.reasoning_state`).

    Attributes:
        state: The state, as the block will carry it.
    """

    state: DynamicData
    kind: Literal["reasoning_state"] = "reasoning_state"


@dataclass(frozen=True)
class StreamToolUseStart(
    TaggedDataclass,
    # Provisional version bump: see behaviors/types/namespace.py.
    fxtr_type=behaviors_type("StreamToolUseStart.v2"),
):
    """The model started a tool call (see ContentToolUse).

    Attributes:
        index: Position of this tool call among the response's tool calls,
            used to correlate subsequent input deltas.
        id: Provider-assigned tool-call ID (see ContentToolUse.id).
        name: Name of the tool being invoked.
        namespace: The group the tool was called under, when the provider's
            dialect groups tools (see ContentToolUse.namespace); the assembled
            call carries the same value.
    """

    index: int
    id: str
    name: str
    namespace: str | None = None
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


@dataclass(frozen=True)
class StreamKeepalive(TaggedDataclass, fxtr_type=behaviors_type("StreamKeepalive.v1")):
    """The provider is still sending, but what arrived carries nothing to assemble.

    Emitted for stream bytes that produced no other event, such as a provider's
    keepalive pings. It says only that the stream is alive: consumers that
    want content ignore it, and it never appears in the final response.
    """

    kind: Literal["keepalive"] = "keepalive"


StreamEvent = (
    StreamTextDelta
    | StreamReasoningStart
    | StreamReasoningDelta
    | StreamReasoningState
    | StreamToolUseStart
    | StreamToolUseInputDelta
    | StreamRestart
    | StreamKeepalive
)

type StreamEventCallback = Callable[[StreamEvent], Awaitable[None]]
"""Receives each stream event as it arrives."""


type SubjectStreamItem = StreamEvent | ModelResponse | ModelCallFailure
"""One item of a streamed model call, as the dialect codecs consume it.

Zero or more `StreamEvent` deltas, then exactly one terminal `ModelResponse` or
`ModelCallFailure`, then the end of the stream. Stop reason, usage and the provider response id
travel only on the terminal item, since every dialect reports those at end of stream.
"""
