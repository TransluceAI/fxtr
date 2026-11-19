"""Live acceptance of the schemas each provider is sent: the provider as an oracle independent of
`dialects.schema_support` and the providers' documentation as we read it.

Every schema in the corpus (`schema_corpus`) goes to every provider through its own model API,
so adaptation runs as it does for any caller: as a strict response format, and, for an object
root, as the input schema of a strict tool the model must call; to Gemini also as one the model
may call, since only then does Gemini validate calls (`VALIDATED`, checked on the wire). Two
checks:

- **Accepted.** The provider answers rather than rejecting the request. Adaptation is meant to
  send only what the provider takes (moving, closing, un-enforcing or dropping the rest, with a
  loss), so a 400 here is an adaptation bug: a rule `schema_support` is missing.
- **Kept when nothing was lost.** When adaptation recorded no loss for the schema, it claims the
  provider enforces it whole, so an answer that ended normally must validate against the
  original. (One conforming answer does not prove enforcement; a nonconforming one disproves it.)

The corpus is organized by the JSON Schema features the providers document limits on, each in
the forms and placements that change how a rule applies: a keyword at the root and nested, under
`properties`, `items`, `$defs` and combinators; `type` as a string and as an array; references
local, at the root, recursive and external; property names and example values that look like
keywords. When a provider's rules change, a failing case here is the first sign; add a case for
each new rule or bug, and record what the provider did in `schema_corpus.SWEEP`.

With `BEHAVIORS_RECORD_ACCEPTED_SCHEMAS` set to a path, each accepted case's wire digest is
appended there; that file, sorted, is `schema_corpus.ACCEPTED_PATH`, which `test_schema_corpus`
holds adaptation to offline.

Opt in with `pytest -m llm_integration` and the providers' keys, e.g. `inject openai anthropic
gemini -- pytest -m llm_integration -n 16` on this file; each provider is skipped without its
key. The models are `BEHAVIORS_TEST_{ANTHROPIC,OPENAI,GEMINI}_MODEL`.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jsonschema
import pytest
from jsonschema.validators import validator_for
from referencing import Registry

from behaviors.implementations.dialects.anthropic_messages import AnthropicMessagesCodec
from behaviors.implementations.dialects.gemini_generate_content import GeminiGenerateContentCodec
from behaviors.implementations.dialects.openai_chat_completions import OpenAIChatCompletionsCodec
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesCodec
from behaviors.interfaces.dialect_codecs import ModelSideCodec
from behaviors.interfaces.model_apis import ModelAPI
from behaviors.types.content import ContentText, ContentToolUse
from behaviors.types.failure import ModelCallFailure
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    StopEndTurn,
    StopToolUse,
)

from .schema_corpus import (
    CORPUS,
    KNOWN_LIMITS,
    USES,
    Case,
    key,
    request_for,
    runs,
    wire_digest,
)

pytestmark = [pytest.mark.anyio, pytest.mark.llm_integration]


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


@dataclass(frozen=True)
class Provider:
    name: str
    make_api: Callable[[], ModelAPI]
    codec: ModelSideCodec


def _provider(provider: Provider, key: str) -> Any:
    return pytest.param(
        provider,
        id=provider.name,
        marks=pytest.mark.skipif(not os.environ.get(key), reason=f"needs {key}"),
    )


_PROVIDERS = [
    _provider(
        Provider("anthropic", _anthropic, AnthropicMessagesCodec()),
        "ANTHROPIC_API_KEY",
    ),
    _provider(
        Provider("openai_responses", _responses, OpenAIResponsesCodec()),
        "OPENAI_API_KEY",
    ),
    _provider(
        Provider("openai_chat_completions", _chat_completions, OpenAIChatCompletionsCodec()),
        "OPENAI_API_KEY",
    ),
    _provider(Provider("gemini", _gemini, GeminiGenerateContentCodec()), "GEMINI_API_KEY"),
]


def _answer(response: ModelResponse, use: str) -> Any:
    """The value the model gave, or None when the response did not end with one."""
    blocks = [block for message in response.output for block in message.content]
    if use != "response_format":
        if not isinstance(response.stop_reason, StopToolUse | StopEndTurn):
            return None
        calls = [block for block in blocks if isinstance(block, ContentToolUse)]
        return dict(calls[0].input) if calls and calls[0].parse_error is None else None
    if not isinstance(response.stop_reason, StopEndTurn):
        return None
    return json.loads("".join(block.text for block in blocks if isinstance(block, ContentText)))


def _record_accepted(provider: Provider, case: Case, use: str, request: ModelRequest) -> None:
    path = os.environ.get("BEHAVIORS_RECORD_ACCEPTED_SCHEMAS")
    if path:
        entry = {
            "key": key(provider.name, case, use),
            "digest": wire_digest(provider.codec, request),
        }
        with open(path, "a") as out:  # one short line per write: safe across workers
            out.write(json.dumps(entry) + "\n")


@pytest.mark.parametrize("use", USES)
@pytest.mark.parametrize("case", CORPUS, ids=[case.name for case in CORPUS])
@pytest.mark.parametrize("provider", _PROVIDERS)
async def test_a_provider_accepts_every_schema_it_is_sent(
    provider: Provider, case: Case, use: str
) -> None:
    if not runs(provider.name, case, use):
        pytest.skip("not sent: a tool's schema is an object; auto_tool is Gemini's")
    known = KNOWN_LIMITS.get((provider.name, case.name, use))
    request = request_for(case, use)
    adapted = provider.codec.adapt_request("m", request)
    losses = adapted.losses
    if use == "auto_tool" and adapted.request.tools[0].strict is True:
        # Gemini enforces a tool's schema only by validating calls; check that is what is sent.
        body: Any = provider.codec.encode_request("m", adapted.request, streaming=False).body
        assert body["toolConfig"]["functionCallingConfig"]["mode"] == "VALIDATED"
    api: Any = provider.make_api()
    try:
        result = await api.generate(request)
    finally:
        await api.aclose()
    if isinstance(result, ModelCallFailure):
        if (
            known is not None
            and result.provider_http_status == 400
            and known.signature in result.description
        ):
            pytest.xfail(f"{known.reason}: {result.description}")
        pytest.fail(
            f"rejected ({result.category}, {result.provider_http_status}): "
            f"{result.description}\nlosses: {[loss.code for loss in losses]}"
        )
    assert isinstance(result, ModelResponse)
    _record_accepted(provider, case, use, request)
    if losses:
        return  # adaptation said what the provider does not enforce
    value = _answer(result, use)
    if value is None:
        return
    validator: Any = validator_for(case.schema, default=jsonschema.Draft202012Validator)
    errors = [
        error.message for error in validator(case.schema, registry=Registry()).iter_errors(value)
    ]
    assert not errors, f"no loss was recorded, but the answer does not conform: {errors}\n{value}"
