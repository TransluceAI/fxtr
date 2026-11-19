"""Live checks that each provider accepts the enforced forms the codecs render for a response
schema and a strict tool: Anthropic `output_config.format`, OpenAI `strict: true` (Responses and
chat completions), and Gemini `responseJsonSchema` with `VALIDATED`.

These show that the requests are accepted and that the answers conform. One conforming answer
does not show that the provider enforced the schema; that the request asked for enforcement is
what the unit tests check, on the wire form.

Opt in with `pytest -m llm_integration` and the providers' keys (e.g.
`inject openai anthropic -- pytest -m llm_integration`); each provider is skipped without its
key. The models are `BEHAVIORS_TEST_{ANTHROPIC,OPENAI,GEMINI}_MODEL`.
"""

import json
import os
from collections.abc import Callable
from typing import Any

import jsonschema
import pytest

from behaviors.interfaces.model_apis import ModelAPI
from behaviors.types.content import ContentText, ContentToolUse
from behaviors.types.json import JSONObject
from behaviors.types.message import UserMessage
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ResponseFormatJsonSchema,
    SamplingParams,
)
from behaviors.types.tool import ToolParam

pytestmark = [pytest.mark.anyio, pytest.mark.llm_integration]

_SCHEMA: JSONObject = {
    "type": "object",
    "properties": {
        "capital": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["capital", "confidence"],
    "additionalProperties": False,
}


def _anthropic() -> ModelAPI:
    from behaviors.implementations.models.anthropic import AnthropicMessagesAPI

    return AnthropicMessagesAPI(
        os.environ.get("BEHAVIORS_TEST_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    )


def _responses() -> ModelAPI:
    from behaviors.implementations.models.openai_responses import OpenAIResponsesAPI

    return OpenAIResponsesAPI(os.environ.get("BEHAVIORS_TEST_OPENAI_MODEL", "gpt-4o-mini"))


def _chat_completions() -> ModelAPI:
    from behaviors.implementations.models.openai_compatible import OpenAICompatibleAPI

    return OpenAICompatibleAPI(os.environ.get("BEHAVIORS_TEST_OPENAI_MODEL", "gpt-4o-mini"))


def _gemini() -> ModelAPI:
    from behaviors.implementations.models.gemini import GeminiGenerateContentAPI

    return GeminiGenerateContentAPI(
        os.environ.get("BEHAVIORS_TEST_GEMINI_MODEL", "gemini-3.8-flash")
    )


_PROVIDERS = [
    pytest.param(
        _anthropic,
        id="anthropic",
        marks=pytest.mark.skipif(
            not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY"
        ),
    ),
    pytest.param(
        _responses,
        id="openai_responses",
        marks=pytest.mark.skipif(
            not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY"
        ),
    ),
    pytest.param(
        _chat_completions,
        id="openai_chat_completions",
        marks=pytest.mark.skipif(
            not os.environ.get("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY"
        ),
    ),
    pytest.param(
        _gemini,
        id="gemini",
        marks=pytest.mark.skipif(
            not os.environ.get("GEMINI_API_KEY"), reason="needs GEMINI_API_KEY"
        ),
    ),
]


async def _generate(make_api: Callable[[], ModelAPI], request: ModelRequest) -> ModelResponse:
    api: Any = make_api()
    try:
        response = await api.generate(request)
    finally:
        await api.aclose()
    assert isinstance(response, ModelResponse), response
    return response


@pytest.mark.parametrize("make_api", _PROVIDERS)
async def test_a_strict_response_schema_is_accepted_and_answered(
    make_api: Callable[[], ModelAPI],
) -> None:
    request = ModelRequest(
        context=(UserMessage(content=(ContentText(text="What is the capital of France?"),)),),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(max_tokens=512),
        response_format=ResponseFormatJsonSchema(schema=_SCHEMA, strict=True, name="answer"),
    )
    response = await _generate(make_api, request)
    text = "".join(
        block.text for block in response.output[0].content if isinstance(block, ContentText)
    )
    jsonschema.validate(json.loads(text), dict(_SCHEMA))


@pytest.mark.parametrize("make_api", _PROVIDERS)
async def test_a_strict_tool_is_accepted(make_api: Callable[[], ModelAPI]) -> None:
    tool = ToolParam(
        name="record_capital",
        description="Record a country's capital.",
        input_schema=_SCHEMA,
        strict=True,
        input_examples=None,
    )
    request = ModelRequest(
        context=(
            UserMessage(content=(ContentText(text="Record the capital of France with the tool."),)),
        ),
        tools=(tool,),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(max_tokens=512),
    )
    response = await _generate(make_api, request)
    calls = [block for block in response.output[0].content if isinstance(block, ContentToolUse)]
    assert calls, "the model did not call the tool"
    jsonschema.validate(dict(calls[0].input), dict(_SCHEMA))
