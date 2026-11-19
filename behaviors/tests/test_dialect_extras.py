"""Dialect extras: what a harness request carries that the model vocabulary has no slot for.

Each harness-side decoder attaches the leftover as `ModelRequest.extras` (leaf-path subtraction
for parameter objects the vocabulary models partially, whole keys otherwise, headers and query
classified by function); the same dialect's encoder merges the bag back in. A bag of another
dialect is ignored by the codec, and a merge that would overwrite a rendered leaf is a codec bug.
"""

import json
from dataclasses import replace
from typing import Any, cast

import pytest

from behaviors.implementations.dialects import (
    AnthropicMessagesCodec,
    GeminiGenerateContentCodec,
    OpenAIChatCompletionsCodec,
    OpenAIResponsesCodec,
)
from behaviors.implementations.dialects._common import BodyReader, leftover_headers
from behaviors.interfaces.dialect_codecs import CodecError
from behaviors.types.content import ContentText
from behaviors.types.dialect import DialectExtras, Headers
from behaviors.types.json import JSONObject
from behaviors.types.message import UserMessage
from behaviors.types.response import ModelRequest, ReasoningParams, SamplingParams

_ANTHROPIC = AnthropicMessagesCodec(default_max_tokens=1000)
_RESPONSES = OpenAIResponsesCodec()
_GEMINI = GeminiGenerateContentCodec()

_HEADERS: Headers = (
    ("Host", "proxy.test"),
    ("Content-Type", "application/json"),
    ("Content-Length", "123"),
    ("Accept-Encoding", "gzip"),
    ("Authorization", "Bearer run-key"),
    ("x-api-key", "run-key"),
    ("x-goog-api-key", "run-key"),
    ("openai-organization", "org_1"),
    ("User-Agent", "harness/1.0"),
    ("X-Stainless-Lang", "js"),
    ("x-app", "cli"),
    ("x-goog-api-client", "genai-js/1.0"),
    ("anthropic-version", "2023-06-01"),
    ("anthropic-beta", "context-management-2025-06-27,interleaved-thinking-2025-05-14"),
    ("openai-beta", "responses=v1"),
    ("x-custom-flag", "on"),
    ("Proxy-Authorization", "Basic aGFybmVzczpzZWNyZXQ="),
    ("Connection", "keep-alive, X-Hop-Token"),
    ("X-Hop-Token", "per-connection"),
)


def _anthropic_body(**overrides: Any) -> dict[str, Any]:
    return {
        "model": "claude-test",
        "max_tokens": 8,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        **overrides,
    }


def _decode_anthropic(body: dict[str, Any], target: str = "/v1/messages") -> ModelRequest:
    return _ANTHROPIC.decode_request(target, _HEADERS, json.dumps(body).encode()).request


# --- the body reader ------------------------------------------------------------------------------


def test_body_reader_subtracts_wholly_and_by_leaf() -> None:
    reader = BodyReader(
        {
            "model": "m",
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": "high"},
            "metadata": {"user_id": "u"},
            "empty": {},
        }
    )
    assert reader.take("model") == "m"
    assert reader.peek("thinking", "type") == "adaptive"
    assert reader.take_leaf("thinking", "type") == "adaptive"
    assert reader.take_leaf("output_config", "effort") == "high"
    assert reader.take_leaf("missing", "leaf") is None
    assert reader.peek("thinking", "display", "deeper") is None  # not an object
    assert reader.leftover() == {
        "thinking": {"display": "summarized"},  # the leftover leaf, in place
        # output_config was emptied by leaf consumption and disappears; an object empty from the
        # start stays, as does everything never read.
        "metadata": {"user_id": "u"},
        "empty": {},
    }


def test_body_reader_keeps_a_non_object_whose_leaves_were_consumed() -> None:
    reader = BodyReader({"thinking": "adaptive"})
    assert reader.take_leaf("thinking", "type") is None
    assert reader.leftover() == {"thinking": "adaptive"}


def test_proxy_credentials_and_connection_named_headers_are_never_extras() -> None:
    kept = {name.lower() for name, _ in leftover_headers(_HEADERS)}
    assert kept == {"anthropic-version", "anthropic-beta", "openai-beta", "x-custom-flag"}
    assert leftover_headers(_HEADERS, contract=("anthropic-version",)) == (
        ("anthropic-beta", "context-management-2025-06-27,interleaved-thinking-2025-05-14"),
        ("openai-beta", "responses=v1"),
        ("x-custom-flag", "on"),
    )


# --- Anthropic decode -----------------------------------------------------------------------------


def test_anthropic_extras_carry_leftover_leaves_unknown_keys_flags_and_query() -> None:
    request = _decode_anthropic(
        _anthropic_body(
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "high", "future": 1},
            metadata={"user_id": "u"},
            context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
            service_tier="auto",
            top_k=5,
        ),
        target="/v1/messages?beta=true",
    )
    assert request.sampling.reasoning == ReasoningParams(effort="high", summary=True)
    assert request.sampling.max_tokens == 8
    assert request.extras == DialectExtras(
        dialect="anthropic_messages",
        headers=(
            ("anthropic-beta", "context-management-2025-06-27,interleaved-thinking-2025-05-14"),
            (
                "openai-beta",
                "responses=v1",
            ),  # unknown to this dialect: nothing is assumed diagnostic
            ("x-custom-flag", "on"),
        ),
        query=(("beta", "true"),),
        body={
            "output_config": {"future": 1},
            "metadata": {"user_id": "u"},
            "context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]},
            "service_tier": "auto",
            "top_k": 5,
        },
    )


def test_anthropic_consumes_only_the_leaves_it_could_map() -> None:
    def extras_body(**overrides: Any) -> JSONObject:
        extras = _decode_anthropic(_anthropic_body(**overrides)).extras
        return extras.body if extras is not None else {}

    # Fully mapped thinking configs leave nothing behind.
    assert "thinking" not in extras_body(thinking={"type": "enabled", "budget_tokens": 1024})
    assert "thinking" not in extras_body(thinking={"type": "disabled"})
    # An unknown type, a non-integer budget, an effort without thinking or with thinking off,
    # and an unknown effort level stay for the provider to see.
    assert extras_body(thinking={"type": "weird"})["thinking"] == {"type": "weird"}
    assert extras_body(thinking={"type": "enabled", "budget_tokens": "big"})["thinking"] == {
        "budget_tokens": "big"
    }
    assert extras_body(output_config={"effort": "high"})["output_config"] == {"effort": "high"}
    assert extras_body(thinking={"type": "disabled"}, output_config={"effort": "high"})[
        "output_config"
    ] == {"effort": "high"}
    assert extras_body(thinking={"type": "adaptive"}, output_config={"effort": "ultra"})[
        "output_config"
    ] == {"effort": "ultra"}


def test_no_extras_when_nothing_is_left() -> None:
    plain: Headers = (("content-type", "application/json"), ("x-api-key", "k"))
    decoded = _ANTHROPIC.decode_request(
        "/v1/messages", plain, json.dumps(_anthropic_body()).encode()
    )
    assert decoded.request.extras is None


# --- Anthropic encode -----------------------------------------------------------------------------


def test_anthropic_encode_merges_a_same_dialect_bag() -> None:
    body = _anthropic_body(
        thinking={"type": "adaptive", "display": "summarized"},
        metadata={"user_id": "u"},
        top_k=5,
    )
    decoded = _ANTHROPIC.decode_request(
        "/v1/messages?beta=true", _HEADERS, json.dumps(body).encode()
    )
    encoded = _ANTHROPIC.encode_request("claude-subject", decoded.request, streaming=True)
    assert encoded.body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert encoded.body["metadata"] == {"user_id": "u"}
    assert encoded.body["top_k"] == 5
    assert encoded.body["max_tokens"] == 8  # the harness's value, not the declared default
    assert encoded.headers == (
        ("anthropic-beta", "context-management-2025-06-27,interleaved-thinking-2025-05-14"),
        ("openai-beta", "responses=v1"),
        ("x-custom-flag", "on"),
    )
    assert encoded.query == (("beta", "true"),)


def _request_with_extras(extras: DialectExtras | None) -> ModelRequest:
    return ModelRequest(
        context=(UserMessage(content=(ContentText(text="hi"),)),),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(max_tokens=8),
        extras=extras,
    )


def test_a_bag_of_another_dialect_is_left_out_by_adaptation() -> None:
    foreign = DialectExtras(
        dialect="openai_responses", headers=(("openai-beta", "x"),), body={"store": True}
    )
    request = _request_with_extras(foreign)
    adapted = _ANTHROPIC.adapt_request("m", request)
    assert [loss.code for loss in adapted.losses] == ["extras"]
    encoded = _ANTHROPIC.encode_request("m", adapted.request, streaming=True)
    assert "store" not in encoded.body
    assert encoded.headers == ()
    with pytest.raises(CodecError, match="openai_responses extras"):
        _ANTHROPIC.encode_request("m", request, streaming=True)


def test_encode_refuses_a_bag_that_would_overwrite_a_rendered_leaf() -> None:
    # `max_tokens` is rendered from the vocabulary; a bag carrying it means the decoder failed
    # to consume what the encoder renders, so even an equal value is refused.
    colliding = DialectExtras(dialect="anthropic_messages", body={"max_tokens": 8})
    with pytest.raises(CodecError, match="/max_tokens"):
        _ANTHROPIC.encode_request("m", _request_with_extras(colliding), streaming=True)
    nested = DialectExtras(dialect="anthropic_messages", body={"thinking": {"type": "adaptive"}})
    request = ModelRequest(
        context=(UserMessage(content=(ContentText(text="hi"),)),),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(reasoning=ReasoningParams()),
        extras=nested,
    )
    with pytest.raises(CodecError, match="/thinking/type"):
        _ANTHROPIC.encode_request("m", request, streaming=True)


def test_encode_refuses_a_bag_colliding_with_provider_params() -> None:
    bag = DialectExtras(dialect="anthropic_messages", body={"top_k": 5})
    request = ModelRequest(
        context=(UserMessage(content=(ContentText(text="hi"),)),),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(extra={"top_k": 9}),
        extras=bag,
    )
    with pytest.raises(CodecError, match="/top_k"):
        _ANTHROPIC.encode_request("m", request, streaming=True)


# --- Responses ---------------------------------------------------------------------------------


def _responses_body(**overrides: Any) -> dict[str, Any]:
    return {"model": "gpt-test", "input": "hi", "stream": True, **overrides}


def test_responses_extras_and_declared_defaults_do_not_collide() -> None:
    body = _responses_body(
        reasoning={"effort": "medium", "summary": "detailed"},
        store=True,
        include=["reasoning.encrypted_content"],
        prompt_cache_key="cache-1",
        text={"verbosity": "low"},
    )
    decoded = _RESPONSES.decode_request("/v1/responses", _HEADERS, json.dumps(body).encode())
    request = decoded.request
    # `summary: detailed` is the mode the vocabulary spells: consumed into the choice.
    assert request.sampling.reasoning == ReasoningParams(effort="medium", summary=True)
    assert request.extras is not None
    assert request.extras.dialect == "openai_responses"
    assert request.extras.body == {
        "store": True,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": "cache-1",
        "text": {"verbosity": "low"},
    }
    assert ("openai-beta", "responses=v1") in request.extras.headers
    assert ("openai-organization", "org_1") not in request.extras.headers
    encoded = _RESPONSES.encode_request("gpt-subject", request, streaming=True)
    # The harness's own summary mode and store setting win over the declared defaults.
    assert encoded.body["reasoning"] == {"effort": "medium", "summary": "detailed"}
    assert encoded.body["store"] is True
    assert encoded.body["include"] == ["reasoning.encrypted_content"]
    assert encoded.body["prompt_cache_key"] == "cache-1"
    assert encoded.body["text"] == {"verbosity": "low"}


def test_responses_reasoning_summary_auto_round_trips_without_a_collision() -> None:
    body = _responses_body(reasoning={"effort": "medium", "summary": "auto"})
    decoded = _RESPONSES.decode_request("/v1/responses", (), json.dumps(body).encode())
    encoded = _RESPONSES.encode_request("gpt-subject", decoded.request, streaming=True)
    assert encoded.body["reasoning"] == {"effort": "medium", "summary": "auto"}


def test_responses_unmapped_effort_stays_in_extras() -> None:
    body = _responses_body(reasoning={"effort": "ultra"})
    decoded = _RESPONSES.decode_request("/v1/responses", (), json.dumps(body).encode())
    assert decoded.request.sampling.reasoning == ReasoningParams(effort=None)
    assert decoded.request.extras is not None
    assert decoded.request.extras.body == {"reasoning": {"effort": "ultra"}}
    encoded = _RESPONSES.encode_request("gpt-subject", decoded.request, streaming=True)
    assert encoded.body["reasoning"] == {"effort": "ultra", "summary": "auto"}


# --- Gemini ------------------------------------------------------------------------------------


def test_gemini_extras_keep_generation_leaves_and_drop_the_credential_query() -> None:
    body = {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {
            "temperature": 1,
            "topP": 0.95,
            "topK": 64,
            "thinkingConfig": {"includeThoughts": True, "thinkingBudget": 8192},
        },
        "safetySettings": [{"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"}],
    }
    target = "/v1beta/models/gemini-test:streamGenerateContent?alt=sse&key=run-key&trace=1"
    decoded = _GEMINI.decode_request(target, _HEADERS, json.dumps(body).encode())
    request = decoded.request
    assert request.sampling.reasoning == ReasoningParams(budget_tokens=8192, summary=True)
    assert request.sampling.top_p == 0.95
    assert request.extras == DialectExtras(
        dialect="gemini_generate_content",
        headers=(
            ("anthropic-version", "2023-06-01"),  # another dialect's contract header: unknown here
            ("anthropic-beta", "context-management-2025-06-27,interleaved-thinking-2025-05-14"),
            ("openai-beta", "responses=v1"),
            ("x-custom-flag", "on"),
        ),
        query=(("trace", "1"),),
        body={
            "generationConfig": {"topK": 64},
            "safetySettings": [
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"}
            ],
        },
    )


# --- chat completions (no harness side; a bag still applies) -----------------------------------


def test_chat_completions_encode_applies_a_same_dialect_bag() -> None:
    bag = DialectExtras(
        dialect="openai_chat_completions",
        headers=(("openai-beta", "x"),),
        body={"logit_bias": {"50256": -100}},
    )
    encoded = OpenAIChatCompletionsCodec().encode_request(
        "m", _request_with_extras(bag), streaming=True
    )
    assert encoded.body["logit_bias"] == {"50256": -100}
    assert encoded.headers == (("openai-beta", "x"),)
    assert cast(dict[str, Any], encoded.body)["max_tokens"] == 8


# --- stripping the harness's reasoning configuration ---------------------------------------------


def test_strip_reasoning_removes_the_configurations_leftover_leaves_too() -> None:
    request = _decode_anthropic(
        _anthropic_body(
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "high"},
            metadata={"user_id": "u"},
        )
    )
    stripped = _ANTHROPIC.strip_reasoning(request)
    assert stripped.sampling.reasoning is None
    assert stripped.extras is not None
    assert stripped.extras.body == {"metadata": {"user_id": "u"}}  # display went with its config

    # Only the effort leaf of output_config is reasoning configuration; structured-output
    # settings next to it stay.
    structured = _decode_anthropic(
        _anthropic_body(output_config={"effort": "high", "format": {"type": "json_schema"}})
    )
    stripped = _ANTHROPIC.strip_reasoning(structured)
    assert stripped.extras is not None
    assert stripped.extras.body == {"output_config": {"format": {"type": "json_schema"}}}
    assert "thinking" not in _ANTHROPIC.encode_request("m", stripped, streaming=True).body

    body = _responses_body(reasoning={"effort": "medium", "summary": "detailed"})
    decoded = _RESPONSES.decode_request("/v1/responses", (), json.dumps(body).encode())
    assert _RESPONSES.strip_reasoning(decoded.request).extras is None

    gemini = _GEMINI.decode_request(
        "/v1beta/models/g:generateContent",
        (),
        json.dumps(
            {
                "contents": [],
                "generationConfig": {"thinkingConfig": {"includeThoughts": True}, "topK": 3},
            }
        ).encode(),
    )
    stripped = _GEMINI.strip_reasoning(gemini.request)
    assert stripped.extras is not None
    assert stripped.extras.body == {"generationConfig": {"topK": 3}}


def _responses_request(reasoning: dict[str, Any]) -> ModelRequest:
    return _RESPONSES.decode_request(
        "/v1/responses", (), json.dumps(_responses_body(reasoning=reasoning)).encode()
    ).request


def _with_summary(request: ModelRequest, summary: bool | None) -> ModelRequest:
    """A decoded request with its neutral summary choice overridden (what `SamplingOverrides`
    does on a harness request)."""
    reasoning = request.sampling.reasoning
    assert reasoning is not None
    return replace(
        request, sampling=replace(request.sampling, reasoning=replace(reasoning, summary=summary))
    )


def _encoded_reasoning(request: ModelRequest) -> JSONObject:
    body = _RESPONSES.encode_request("gpt-subject", request, streaming=True).body
    return cast("JSONObject", body["reasoning"])


def test_responses_summary_modes_beside_the_choice() -> None:
    # `auto` and `concise` carry more than the neutral choice, so they stay extras beside it and
    # a same-dialect subject gets the mode verbatim; the choice alone renders `detailed`.
    for mode in ("auto", "concise"):
        request = _responses_request({"effort": "medium", "summary": mode})
        assert request.sampling.reasoning == ReasoningParams(effort="medium", summary=True)
        assert request.extras is not None and request.extras.body["reasoning"] == {"summary": mode}
        assert _encoded_reasoning(request) == {"effort": "medium", "summary": mode}


def test_responses_summary_override_wins_over_a_retained_mode() -> None:
    # Decode → override the neutral choice → encode: an explicit omission removes a retained
    # mode, an explicit request keeps a compatible one, and unspecified honors it.
    request = _responses_request({"effort": "medium", "summary": "auto"})
    assert _encoded_reasoning(_with_summary(request, False)) == {"effort": "medium"}
    assert _encoded_reasoning(_with_summary(request, True)) == {
        "effort": "medium",
        "summary": "auto",
    }
    assert _encoded_reasoning(_with_summary(request, None)) == {
        "effort": "medium",
        "summary": "auto",
    }
    # Nothing retained: the choice renders the neutral spelling, or the declared default.
    bare = _responses_request({"effort": "medium"})
    assert _encoded_reasoning(_with_summary(bare, True)) == {
        "effort": "medium",
        "summary": "detailed",
    }
    assert _encoded_reasoning(_with_summary(bare, False)) == {"effort": "medium"}
    assert _encoded_reasoning(bare) == {"effort": "medium", "summary": "auto"}


def test_responses_explicit_null_summary_is_the_opt_out_and_is_kept_as_spelled() -> None:
    # The API treats summaries as opt-in, so `summary: null` is an explicit opt-out: it decodes
    # to the choice False, stays in the extras as spelled, reconstructs as null (not the
    # declared default), and only an explicit True overrides it.
    request = _responses_request({"effort": "medium", "summary": None})
    assert request.sampling.reasoning == ReasoningParams(effort="medium", summary=False)
    assert request.extras is not None and request.extras.body["reasoning"] == {"summary": None}
    assert _encoded_reasoning(request) == {"effort": "medium", "summary": None}
    assert _encoded_reasoning(_with_summary(request, None)) == {"effort": "medium", "summary": None}
    assert _encoded_reasoning(_with_summary(request, True)) == {
        "effort": "medium",
        "summary": "detailed",
    }


def test_disabling_reasoning_takes_precedence_over_a_summary_choice() -> None:
    # Decode a request with a retained summary control, then disable reasoning: an explicit
    # summary choice drops the retained control with it; an unspecified one keeps what the
    # harness sent, so a request that arrived with reasoning off reconstructs as sent.
    request = _responses_request({"effort": "medium", "summary": "auto"})
    for choice in (False, True):
        off = replace(
            request,
            sampling=replace(
                request.sampling, reasoning=ReasoningParams(enabled=False, summary=choice)
            ),
        )
        assert _encoded_reasoning(off) == {"effort": "none"}
    off_unspecified = replace(
        request, sampling=replace(request.sampling, reasoning=ReasoningParams(enabled=False))
    )
    assert _encoded_reasoning(off_unspecified) == {"effort": "none", "summary": "auto"}
    as_sent = _responses_request({"effort": "none", "summary": "auto"})
    assert as_sent.sampling.reasoning == ReasoningParams(enabled=False)
    assert _encoded_reasoning(as_sent) == {"effort": "none", "summary": "auto"}

    decoded = _ANTHROPIC.decode_request(
        "/v1/messages",
        (),
        json.dumps(_anthropic_body(thinking={"type": "adaptive", "display": "verbose"})).encode(),
    ).request
    off = replace(
        decoded,
        sampling=replace(decoded.sampling, reasoning=ReasoningParams(enabled=False, summary=True)),
    )
    body = _ANTHROPIC.encode_request("claude-subject", off, streaming=True).body
    assert body["thinking"] == {"type": "disabled"}
    off_unspecified = replace(
        decoded, sampling=replace(decoded.sampling, reasoning=ReasoningParams(enabled=False))
    )
    body = _ANTHROPIC.encode_request("claude-subject", off_unspecified, streaming=True).body
    assert body["thinking"] == {"type": "disabled", "display": "verbose"}


def test_anthropic_summary_override_wins_over_a_retained_display() -> None:
    def encoded_thinking(thinking: dict[str, Any], summary: bool | None) -> JSONObject:
        decoded = _ANTHROPIC.decode_request(
            "/v1/messages", (), json.dumps(_anthropic_body(thinking=thinking)).encode()
        )
        request = decoded.request
        if summary is not None:
            reasoning = request.sampling.reasoning
            assert reasoning is not None
            request = replace(
                request,
                sampling=replace(request.sampling, reasoning=replace(reasoning, summary=summary)),
            )
        body = _ANTHROPIC.encode_request("claude-subject", request, streaming=True).body
        return cast("JSONObject", body["thinking"])

    summarized = {"type": "adaptive", "display": "summarized"}
    assert encoded_thinking(summarized, False) == {"type": "adaptive", "display": "omitted"}
    assert encoded_thinking(summarized, True) == summarized
    # An unknown retained value is not a compatible mode: an explicit choice replaces it, and
    # only an unspecified choice keeps it for the provider to validate.
    unknown = {"type": "adaptive", "display": "verbose"}
    assert encoded_thinking(unknown, True) == summarized
    assert encoded_thinking(unknown, False) == {"type": "adaptive", "display": "omitted"}
    assert encoded_thinking(unknown, None) == unknown


def test_anthropic_unknown_display_value_stays_an_extra_beside_no_choice() -> None:
    decoded = _ANTHROPIC.decode_request(
        "/v1/messages",
        (),
        json.dumps(_anthropic_body(thinking={"type": "adaptive", "display": "verbose"})).encode(),
    )
    request = decoded.request
    assert request.sampling.reasoning == ReasoningParams()
    assert request.extras is not None and request.extras.body["thinking"] == {"display": "verbose"}
    encoded = _ANTHROPIC.encode_request("claude-subject", request, streaming=True)
    assert encoded.body["thinking"] == {"type": "adaptive", "display": "verbose"}
