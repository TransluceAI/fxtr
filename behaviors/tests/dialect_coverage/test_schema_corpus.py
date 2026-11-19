"""Adaptation held to what the providers did with the schema corpus when it was sent live
(`schema_corpus.SWEEP`): a schema a provider rejected is never sent as given (nor, if it rejected
it unenforced too, with only strict mode turned off), and one whose answers broke it is never
sent without a loss saying what is not enforced. No network; the live test is
`test_schema_acceptance_llm_integration`.
"""

from typing import Any

import pytest

from behaviors.implementations.dialects.anthropic_messages import AnthropicMessagesCodec
from behaviors.implementations.dialects.gemini_generate_content import GeminiGenerateContentCodec
from behaviors.implementations.dialects.openai_chat_completions import OpenAIChatCompletionsCodec
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesCodec
from behaviors.interfaces.dialect_codecs import ModelSideCodec
from behaviors.types.response import ModelRequest, ResponseFormatJsonSchema

from .schema_corpus import (
    ACCEPTED_PATH,
    CORPUS,
    KNOWN_LIMITS,
    SWEEP,
    USES,
    Case,
    accepted,
    key,
    request_for,
    runs,
    wire_digest,
)

_CODECS: dict[str, ModelSideCodec] = {
    "anthropic": AnthropicMessagesCodec(),
    "openai_responses": OpenAIResponsesCodec(),
    "openai_chat_completions": OpenAIChatCompletionsCodec(),
    "gemini": GeminiGenerateContentCodec(),
}
_CASES = {case.name: case for case in CORPUS}
_RECORDED = [
    (provider, name, use, outcome)
    for provider, outcomes in SWEEP.items()
    for (name, use), outcome in sorted(outcomes.items())
]


def test_the_sweep_names_cases_in_the_corpus() -> None:
    assert {name for _, name, _, _ in _RECORDED} <= set(_CASES)


@pytest.mark.parametrize(
    ("provider", "name", "use", "outcome"),
    _RECORDED,
    ids=[f"{provider}-{name}-{use}" for provider, name, use, _ in _RECORDED],
)
def test_what_a_provider_did_not_take_is_adapted_with_a_loss(
    provider: str, name: str, use: str, outcome: str
) -> None:
    request = request_for(_CASES[name], use)
    adapted = _CODECS[provider].adapt_request("m", request)
    assert adapted.losses, f"{provider} {outcome} this {use} schema; adaptation sent it as given"
    if outcome == "rejected":  # sent as given, it would be rejected again
        assert adapted.request != request
    if outcome == "rejected unenforced":  # only a different schema, or none, avoids it
        assert _schemas(adapted.request) != _schemas(request)


def _schemas(request: ModelRequest) -> tuple[object, ...]:
    fmt = request.response_format
    return (
        fmt.schema if isinstance(fmt, ResponseFormatJsonSchema) else None,
        *(tool.input_schema for tool in request.tools),
    )


_SENT = [
    (provider, case, use)
    for provider in _CODECS
    for case in CORPUS
    for use in USES
    if runs(provider, case, use) and (provider, case.name, use) not in KNOWN_LIMITS
]


@pytest.mark.parametrize(
    ("provider", "case", "use"),
    _SENT,
    ids=[f"{provider}-{case.name}-{use}" for provider, case, use in _SENT],
)
def test_every_case_is_sent_in_a_form_the_provider_took(
    provider: str, case: Case, use: str
) -> None:
    taken = accepted().get(key(provider, case, use))
    sent = wire_digest(_CODECS[provider], request_for(case, use))
    assert taken == sent, (
        f"{provider} has not been seen to take this {use} form of {case.name!r}: re-run "
        f"test_schema_acceptance_llm_integration recording to {ACCEPTED_PATH.name}"
    )


_AUTO = [case for case in CORPUS if runs("gemini", case, "auto_tool")]


@pytest.mark.parametrize("case", _AUTO, ids=[case.name for case in _AUTO])
def test_a_strict_tool_gemini_may_call_is_validated(case: Case) -> None:
    codec = _CODECS["gemini"]
    adapted = codec.adapt_request("m", request_for(case, "auto_tool"))
    if adapted.request.tools[0].strict is not True:
        assert "tool_strict_unenforced" in {loss.code for loss in adapted.losses}
        return
    body: Any = codec.encode_request("m", adapted.request, streaming=False).body
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "VALIDATED"
