"""Live checks of ``GeminiGenerateContentAPI`` against the Gemini API: what the codec renders is
what the API takes, and what the API returns decodes.

Opt in with `pytest -m llm_integration` (never CI). Needs `GEMINI_API_KEY`; override the models
with `BEHAVIORS_TEST_GEMINI_MODEL` (a thinking model that validates thought signatures and takes
`thinkingBudget: 0`, Gemini 3 or later) and `BEHAVIORS_TEST_GEMINI_LITE_MODEL` (one that refuses
that budget and takes the `MINIMAL` level instead).
"""

import os

import pytest

from behaviors.implementations.models.gemini import GeminiGenerateContentAPI
from behaviors.types.content import ContentReasoning, ContentText, ContentToolUse
from behaviors.types.message import AssistantMessage, ChatMessage, ToolMessage, UserMessage
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ReasoningParams,
    SamplingParams,
    StopToolUse,
)
from behaviors.types.tool import ToolChoiceRequired, ToolParam

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.llm_integration,
    pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="needs GEMINI_API_KEY"),
]

MODEL = os.environ.get("BEHAVIORS_TEST_GEMINI_MODEL", "gemini-3.8-flash")
LITE_MODEL = os.environ.get("BEHAVIORS_TEST_GEMINI_LITE_MODEL", "gemini-3.5-flash-lite")

_WEATHER = ToolParam(
    name="get_weather",
    description="The current weather in a city.",
    input_schema={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
    strict=False,
    input_examples=None,
)
_ASK = UserMessage(content=(ContentText(text="What's the weather in Paris? Use the tool."),))


def _request(*context: ChatMessage, reasoning: ReasoningParams | None = None) -> ModelRequest:
    return ModelRequest(
        context=context,
        tools=(_WEATHER,),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(max_tokens=2048, reasoning=reasoning),
    )


def _call(response: ModelResponse) -> ContentToolUse:
    (message,) = response.output
    assert isinstance(message, AssistantMessage)
    calls = [block for block in message.content if isinstance(block, ContentToolUse)]
    assert calls, message.content
    return calls[0]


async def test_a_tool_loop_round_trips_its_thought_signature() -> None:
    # The API returns a signature with the call; sent back with the result, the next turn is
    # accepted (Gemini 3 refuses a call without its signature).
    api = GeminiGenerateContentAPI(MODEL)
    try:
        first = await api.generate(_request(_ASK, reasoning=ReasoningParams(summary=True)))
        assert isinstance(first, ModelResponse), first
        assert first.stop_reason == StopToolUse()
        (message,) = first.output
        assert isinstance(message, AssistantMessage)
        assert any(
            isinstance(block, ContentReasoning) and block.reasoning_state is not None
            for block in message.content
        ), message.content
        call = _call(first)
        result = ToolMessage(
            content=(ContentText(text="Sunny, 20C"),), tool_call_id=call.id, is_error=None
        )
        second = await api.generate(_request(_ASK, message, result))
        assert isinstance(second, ModelResponse), second
        assert any(
            isinstance(block, ContentText) and block.text
            for out in second.output
            if isinstance(out, AssistantMessage)
            for block in out.content
        )
    finally:
        await api.aclose()


async def test_a_call_another_model_made_is_accepted_with_the_placeholder() -> None:
    # A history whose call carries no signature (as from another provider) is refused, then
    # retried with the placeholder signature.
    api = GeminiGenerateContentAPI(MODEL)
    try:
        call = ContentToolUse(
            id="toolu_1", name="get_weather", input={"city": "Paris"}, parse_error=None
        )
        history = _request(
            _ASK,
            AssistantMessage(content=(call,)),
            ToolMessage(
                content=(ContentText(text="Sunny, 20C"),), tool_call_id="toolu_1", is_error=None
            ),
        )
        result = await api.generate(history)
        assert isinstance(result, ModelResponse), result
    finally:
        await api.aclose()


@pytest.mark.parametrize("model", [MODEL, LITE_MODEL])
async def test_thinking_off_and_a_forced_tool(model: str) -> None:
    # Off is `thinkingBudget: 0` for one model and the MINIMAL level for the other; the model
    # API finds the spelling the model takes.
    api = GeminiGenerateContentAPI(model)
    try:
        request = ModelRequest(
            context=(_ASK,),
            tools=(_WEATHER,),
            tool_choice=ToolChoiceRequired(),
            parallel_tool_calls=None,
            sampling=SamplingParams(max_tokens=1024, reasoning=ReasoningParams(enabled=False)),
        )
        result = await api.generate(request)
        assert isinstance(result, ModelResponse), result
        assert _call(result).name == "get_weather"
        again = await api.generate(request)
        assert isinstance(again, ModelResponse), again
    finally:
        await api.aclose()


async def test_parallel_calls_re_paired_with_their_results_are_accepted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A Responses harness re-pairs each call with its result; rendered back as one turn of
    # calls and one of results, the history is the order the API requires (interleaved, the API
    # refuses it for a call without a signature, which the placeholder retry would then cover:
    # the request must not need it).
    api = GeminiGenerateContentAPI(MODEL)
    try:
        ask = UserMessage(
            content=(
                ContentText(
                    text="What's the weather in Paris and in Rome? Call the tool for both at once."
                ),
            )
        )
        first = await api.generate(_request(ask))
        assert isinstance(first, ModelResponse), first
        (message,) = first.output
        assert isinstance(message, AssistantMessage)
        calls = [block for block in message.content if isinstance(block, ContentToolUse)]
        if len(calls) < 2:
            pytest.skip("the model made one call this time")
        head = message.content[: message.content.index(calls[0]) + 1]
        repaired: list[ChatMessage] = [ask, AssistantMessage(content=head)]
        repaired.append(
            ToolMessage(
                content=(ContentText(text="Sunny"),), tool_call_id=calls[0].id, is_error=None
            )
        )
        for call in calls[1:]:
            repaired.append(AssistantMessage(content=(call,)))
            repaired.append(
                ToolMessage(
                    content=(ContentText(text="Rain"),), tool_call_id=call.id, is_error=None
                )
            )
        second = await api.generate(_request(*repaired))
        assert isinstance(second, ModelResponse), second
        assert "placeholder" not in caplog.text
    finally:
        await api.aclose()
