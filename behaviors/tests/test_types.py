"""Tests for the core chat, content, and tool types."""

import dataclasses

import pytest

from behaviors.types import (
    AssistantMessage,
    CacheableContentText,
    CacheableToolParam,
    CacheControl,
    ChatMessage,
    Content,
    ContentReasoning,
    ContentRedactedReasoning,
    ContentSummarizedReasoning,
    ContentText,
    ContentToolUse,
    ModelCallFailure,
    ModelRequest,
    ModelResponse,
    ReasoningParams,
    SamplingParams,
    StopContentFilter,
    StopEndTurn,
    StopMaxTokens,
    StopOther,
    StopReason,
    StopStopSequence,
    StopToolUse,
    StreamEvent,
    StreamReasoningDelta,
    StreamRestart,
    StreamSignatureDelta,
    StreamSummarizedReasoningDelta,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
    SystemMessage,
    ToolChoice,
    ToolChoiceAuto,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolChoiceTool,
    ToolMessage,
    ToolParam,
    Transcript,
    Usage,
    UserMessage,
)
from fxtr.entity_defns.dataclass_bases import ConversionError
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.storage.memory_storage import MemoryStorage


def test_content_blocks_construct() -> None:
    text = ContentText(text="hello")
    reasoning = ContentReasoning(text="thinking...")
    summarized = ContentSummarizedReasoning(text="summary", signature="sig")
    # OpenAI reasoning item with encrypted_content but no summary text
    empty_summary = ContentSummarizedReasoning(text="", signature="enc")
    unsigned = ContentSummarizedReasoning(text="summary", signature=None)
    redacted = ContentRedactedReasoning(data="0xdeadbeef")
    tool_use = ContentToolUse(
        id="tu_1", name="search", input={"query": "cats", "limit": 5}, parse_error=None
    )

    assert text.text == "hello"
    assert reasoning.text == "thinking..."
    assert summarized.signature == "sig"
    assert empty_summary.text == ""
    assert unsigned.signature is None
    assert redacted.data == "0xdeadbeef"
    assert tool_use.input == {"query": "cats", "limit": 5}


def test_content_blocks_are_frozen() -> None:
    text = ContentText(text="hello")
    with pytest.raises(dataclasses.FrozenInstanceError):
        text.text = "goodbye"  # type: ignore[misc]


def test_messages_construct() -> None:
    system = SystemMessage(content=(ContentText(text="be helpful"),))
    user = UserMessage(content=(ContentText(text="hi"),))
    tool_use = ContentToolUse(id="tu_1", name="search", input={"query": "cats"}, parse_error=None)
    assistant = AssistantMessage(
        content=(
            ContentReasoning(text="the user greeted me"),
            ContentText(text="hello!"),
            tool_use,
        ),
    )
    tool = ToolMessage(content=(ContentText(text="3 results"),), tool_call_id="tu_1", is_error=None)

    messages: tuple[ChatMessage, ...] = (system, user, assistant, tool)
    assert len(messages) == 4
    assert tool.tool_call_id == tool_use.id


def test_content_union_narrows() -> None:
    blocks: tuple[Content, ...] = (
        ContentText(text="a"),
        ContentReasoning(text="b"),
        ContentSummarizedReasoning(text="c", signature="s"),
        ContentRedactedReasoning(data="d"),
        ContentToolUse(id="tu_1", name="f", input={}, parse_error=None),
    )
    texts = [block.text for block in blocks if isinstance(block, ContentText)]
    assert texts == ["a"]


def test_value_equality() -> None:
    assert ContentText(text="x") == ContentText(text="x")
    assert UserMessage(content=(ContentText(text="x"),)) == UserMessage(
        content=(ContentText(text="x"),)
    )
    assert ContentText(text="x") != ContentText(text="y")


def test_tool_param() -> None:
    param = ToolParam(
        name="search",
        description="Search the web",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        strict=True,
        input_examples=({"query": "weather in Paris"},),
    )
    assert param.name == "search"
    assert param.strict is True
    assert param.input_examples == ({"query": "weather in Paris"},)

    no_description = ToolParam(
        name="noop",
        description=None,
        input_schema={"type": "object"},
        strict=None,
        input_examples=None,
    )
    assert no_description.description is None


@dataclasses.dataclass(frozen=True)
class RecordedExchange(DataclassEntity, fxtr_type="org.transluce.behaviors.tests.RecordedExchange"):
    request: ModelRequest
    result: ModelResponse | ModelCallFailure
    events: tuple[StreamEvent, ...]


def _messages() -> tuple[ChatMessage, ...]:
    return (
        SystemMessage(content=(CacheableContentText(text="system", cache_control=CacheControl()),)),
        UserMessage(content=(ContentText(text="search"),)),
        AssistantMessage(
            content=(
                ContentReasoning(text="think"),
                ContentSummarizedReasoning(text="summary", signature="signature"),
                ContentRedactedReasoning(data="encrypted"),
                CacheableContentText(text="answer", cache_control=CacheControl(ttl="1h")),
                ContentToolUse(
                    id="call-1",
                    name="search",
                    input={"query": "cats", "filters": [{"enabled": True, "score": 1.5}]},
                    parse_error=None,
                ),
            )
        ),
        ToolMessage(content=(ContentText(text="found"),), tool_call_id="call-1", is_error=False),
    )


@pytest.mark.anyio
async def test_nested_message_content_and_cache_controls_roundtrip() -> None:
    store = MemoryStorage()
    original = Transcript(name="example", description=None, messages=_messages())
    ref = await store.store(original)
    restored = await store.load(ref)
    assert restored == original
    assert isinstance(restored.messages[0].content[0], CacheableContentText)
    assert restored.messages[0].content[0].cache_control == CacheControl()
    assert isinstance(restored.messages[2].content[3], CacheableContentText)
    assert (await store.store(restored)).entity_id == ref.entity_id
    raw = await store.load_raw_data(ref.entity_id)
    assert isinstance(raw, dict)
    assert raw["$type"] == "org.transluce.behaviors.Transcript.v1"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "choice",
    [None, ToolChoiceAuto(), ToolChoiceNone(), ToolChoiceRequired(), ToolChoiceTool("search")],
)
async def test_exchange_request_and_stream_events_roundtrip(choice: ToolChoice | None) -> None:
    store = MemoryStorage()
    exchange = RecordedExchange(
        request=ModelRequest(
            context=_messages(),
            tools=(
                CacheableToolParam(
                    name="search",
                    description=None,
                    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
                    strict=True,
                    input_examples=({"query": "cats", "n": 1},),
                    cache_control=CacheControl(ttl="5m"),
                ),
            ),
            tool_choice=choice,
            parallel_tool_calls=False,
            sampling=SamplingParams(
                temperature=0.5,
                reasoning=ReasoningParams(effort="high"),
                extra={"seed": 5, "nested": {"values": [True, None, 0.2]}},
            ),
        ),
        result=ModelCallFailure(
            category="rate_limited", description="wait", provider_http_status=429
        ),
        events=(
            StreamTextDelta("hello"),
            StreamReasoningDelta("think"),
            StreamSummarizedReasoningDelta("summary"),
            StreamSignatureDelta("sig"),
            StreamToolUseStart(index=0, id="call-1", name="search"),
            StreamToolUseInputDelta(index=0, partial_json='{"query":'),
            StreamRestart(),
        ),
    )
    restored = await store.load(await store.store(exchange))
    assert restored == exchange
    assert isinstance(restored.request.tools[0], CacheableToolParam)
    assert isinstance(restored.result, ModelCallFailure)
    assert type(restored.request.sampling.extra["seed"]) is int


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stop",
    [
        StopEndTurn(),
        StopMaxTokens(),
        StopStopSequence("</answer>"),
        StopToolUse(),
        StopContentFilter("refusal"),
        StopOther("provider_specific"),
    ],
)
async def test_model_response_stop_reason_roundtrip(stop: StopReason) -> None:
    store = MemoryStorage()
    exchange = RecordedExchange(
        request=ModelRequest(
            context=(),
            tools=(),
            tool_choice=None,
            parallel_tool_calls=None,
            sampling=SamplingParams(),
        ),
        result=ModelResponse(
            id="response-1",
            output=_messages(),
            stop_reason=stop,
            usage=Usage(
                input_tokens=10,
                cache_read_input_tokens=3,
                cache_write_input_tokens=None,
                output_tokens=7,
                reasoning_output_tokens=2,
            ),
        ),
        events=(),
    )
    restored = await store.load(await store.store(exchange))
    assert restored == exchange
    assert isinstance(restored.result, ModelResponse)
    assert type(restored.result.stop_reason) is type(stop)


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ["unknown_tag", "missing_field", "extra_field", "non_json"])
async def test_storage_rejects_invalid_nested_data(mutation: str) -> None:
    store = MemoryStorage()
    original = Transcript(name=None, description=None, messages=_messages())
    ref = await store.store(original)
    raw = await store.load_raw_data(ref.entity_id)
    assert isinstance(raw, dict)
    messages = raw["messages"]
    assert isinstance(messages, list)
    message = messages[0]
    assert isinstance(message, dict)
    if mutation == "unknown_tag":
        message["$type"] = "org.example.UnknownMessage.v1"
    elif mutation == "missing_field":
        del message["role"]
    elif mutation == "extra_field":
        message["extra"] = "unexpected"
    else:
        raw["metadata"] = {"bytes": b"not JSON"}
    invalid = await store.store_raw_data(raw)
    with pytest.raises(ConversionError):
        await store.load(invalid, as_type=Transcript)
