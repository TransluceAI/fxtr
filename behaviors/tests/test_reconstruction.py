"""The reconstruction check (CI; no I/O): decoding and re-encoding a captured harness body with
the same dialect's codec is the identity under the documented normalizations, an injected
difference is reported leaf by leaf with its JSON pointer."""

import json
from pathlib import Path
from typing import Any, cast

import pytest

from behaviors.implementations.dialects import (
    AnthropicMessagesCodec,
    GeminiGenerateContentCodec,
    OpenAIResponsesCodec,
)
from behaviors.implementations.dialects.reconstruction import (
    Difference,
    compare_request,
    compare_response,
    reference_extras,
)
from behaviors.types.dialect import Headers
from behaviors.types.json import JSONObject
from behaviors.types.response import ReasoningParams

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((_FIXTURES / name).read_text()))


def _round_trip_anthropic(body: dict[str, Any]) -> tuple[tuple[Difference, ...], tuple[str, ...]]:
    codec = AnthropicMessagesCodec()
    decoded = codec.decode_request("/v1/messages", (), json.dumps(body).encode())
    encoded = codec.encode_request("claude-subject", decoded.request, streaming=True)
    return compare_request("anthropic_messages", cast("JSONObject", body), encoded.body)


def test_anthropic_fixture_round_trips_to_the_identity() -> None:
    differences, ignored = _round_trip_anthropic(_fixture("anthropic_claude_code_request.json"))
    assert differences == ()
    assert "model (rewritten by design)" in ignored
    assert "string-vs-block content" in ignored


def test_anthropic_declared_default_is_ignored_only_when_the_harness_omitted_it() -> None:
    body = _fixture("anthropic_claude_code_request.json")
    del body["max_tokens"]
    differences, ignored = _round_trip_anthropic(body)
    assert differences == ()
    assert "declared default /max_tokens" in ignored


def test_an_unmodeled_block_is_reported_at_its_pointer() -> None:
    body = _fixture("anthropic_claude_code_request.json")
    body["messages"][0]["content"] = [
        {"type": "text", "text": "look"},
        {"type": "image", "source": {"type": "url", "url": "https://img.test/x.png"}},
    ]
    differences, _ = _round_trip_anthropic(body)
    assert [(d.path, d.kind) for d in differences] == [
        ("/messages/0/content/1", "missing_in_translated")
    ]


def test_responses_fixture_round_trips_except_the_server_side_tool() -> None:
    body = _fixture("codex_responses_request.json")
    codec = OpenAIResponsesCodec()
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    encoded = codec.encode_request("gpt-subject", decoded.request, streaming=True)
    differences, ignored = compare_request(
        "openai_responses", cast("JSONObject", body), encoded.body
    )
    # The server-side web_search tool has no model-vocabulary form: the one documented loss.
    assert [(d.path, d.kind) for d in differences] == [("/tools/2", "missing_in_translated")]
    assert differences[0].harness == {"type": "web_search"}
    assert "instructions as a leading developer item" in ignored


def test_value_differences_and_extras_are_reported() -> None:
    raw: JSONObject = {"a": 1, "b": {"c": [1, 2]}, "model": "x"}
    encoded: JSONObject = {"a": 2, "b": {"c": [1, 2, 3], "d": True}, "model": "y"}
    differences, _ = compare_request("openai_chat_completions", raw, encoded)
    assert differences == (
        Difference("/a", "value_differs", 1, 2),
        Difference("/b/c/2", "extra_in_translated", None, 3),
        Difference("/b/d", "extra_in_translated", None, True),
    )


def test_booleans_are_not_numbers() -> None:
    differences, _ = compare_request(
        "openai_responses", {"parallel_tool_calls": True}, {"parallel_tool_calls": 1}
    )
    assert [(d.path, d.kind) for d in differences] == [("/parallel_tool_calls", "value_differs")]
    differences, _ = compare_request("openai_responses", {"n": 1}, {"n": 1.0})
    assert differences == ()  # both are JSON numbers


def test_reference_extras_mirror_the_builtin_decoders() -> None:
    # The checker's tables must say what each built-in decoder excludes, or the check would
    # report the codec's own contract header and consumed query names as losses.
    headers: Headers = (
        ("x-api-key", "k"),
        ("anthropic-version", "2023-06-01"),
        ("anthropic-beta", "flag"),
        ("user-agent", "h/1"),
        ("x-custom", "1"),
    )
    anthropic = AnthropicMessagesCodec().decode_request(
        "/v1/messages?beta=true", headers, json.dumps({"model": "m", "messages": []}).encode()
    )
    assert anthropic.request.extras is not None
    assert reference_extras("anthropic_messages", headers, "/v1/messages?beta=true") == (
        anthropic.request.extras.headers,
        anthropic.request.extras.query,
    )
    target = "/v1beta/models/g:streamGenerateContent?alt=sse&key=k&trace=1"
    gemini = GeminiGenerateContentCodec().decode_request(
        target, headers, json.dumps({"contents": []}).encode()
    )
    assert gemini.request.extras is not None
    assert reference_extras("gemini_generate_content", headers, target) == (
        gemini.request.extras.headers,
        gemini.request.extras.query,
    )


def test_headers_and_query_extras_are_compared() -> None:
    differences, _ = compare_request(
        "anthropic_messages",
        {},
        {},
        raw_headers=(("anthropic-beta", "a"), ("Anthropic-Beta", "b"), ("x-custom", "1")),
        raw_query=(("beta", "true"), ("flag", "a"), ("flag", "b")),
        encoded_headers=(("anthropic-beta", "a"), ("anthropic-beta", "b")),
        encoded_query=(("beta", "true"), ("flag", "a")),
    )
    assert [(d.path, d.kind) for d in differences] == [
        ("/headers/x-custom", "missing_in_translated"),
        ("/query/flag/1", "missing_in_translated"),  # the second repeated value
    ]


def test_tool_outputs_with_non_text_parts_are_not_folded_into_text() -> None:
    def outputs(parts: list[dict[str, Any]]) -> tuple[Difference, ...]:
        raw = cast(
            "JSONObject",
            {"input": [{"type": "function_call_output", "call_id": "c", "output": parts}]},
        )
        encoded: JSONObject = {
            "input": [{"type": "function_call_output", "call_id": "c", "output": "a"}]
        }
        return compare_request("openai_responses", raw, encoded)[0]

    assert outputs([{"type": "input_text", "text": "a"}]) == ()  # the same tool output, spelled out
    (difference,) = outputs(
        [{"type": "input_text", "text": "a"}, {"type": "input_image", "image_url": "data:..."}]
    )
    assert (difference.path, difference.kind) == ("/input/0/output", "value_differs")


def test_anthropic_response_projection_ignores_generated_fields() -> None:
    raw: JSONObject = {
        "id": "msg_real",
        "type": "message",
        "role": "assistant",
        "model": "claude-real",
        "content": [{"type": "text", "text": "ok", "citations": []}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    encoded: JSONObject = {
        "id": "msg_synthesized",
        "type": "message",
        "role": "assistant",
        "model": "claude-harness",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    differences, ignored = compare_response("anthropic_messages", raw, encoded)
    assert differences == ()
    assert "empty citations" in ignored


def test_anthropic_serving_details_and_caller_are_ignored_with_reasons() -> None:
    # What a live Anthropic call returned on 2026-09-29 beyond what the vocabulary models.
    raw: JSONObject = {
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "run",
                "input": {},
                "caller": {"type": "direct"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_creation_input_tokens": 3,
            "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 3},
            "inference_geo": "not_available",
            "output_tokens_details": {"thinking_tokens": 2},
            "service_tier": "standard",
            "speed": "standard",
        },
    }
    encoded: JSONObject = {
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "run", "input": {}}],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_creation_input_tokens": 3,
            "output_tokens_details": {"thinking_tokens": 2},
            "speed": "standard",
        },
    }
    differences, ignored = compare_response("anthropic_messages", raw, encoded)
    assert differences == ()
    assert "tool_use caller attribution" in ignored
    assert any(entry.startswith("usage serving details") for entry in ignored)
    # The same caller field echoed back in a request is ignored too, and nothing else is.
    request_raw: JSONObject = {
        "messages": [{"role": "assistant", "content": raw["content"]}],
        "max_tokens": 5,
    }
    request_encoded: JSONObject = {
        "messages": [{"role": "assistant", "content": encoded["content"]}],
        "max_tokens": 5,
    }
    differences, _ = compare_request("anthropic_messages", request_raw, request_encoded)
    assert differences == ()


def test_anthropic_thinking_display_default_is_ignored_only_when_the_harness_omitted_it() -> None:
    body = _fixture("anthropic_claude_code_request.json")
    body["thinking"] = {"type": "adaptive"}
    differences, ignored = _round_trip_anthropic(body)
    assert differences == ()
    assert any("declared default" in entry for entry in ignored)
    # A harness that chose a display keeps it, and a different re-encoding would show.
    body["thinking"] = {"type": "adaptive", "display": "omitted"}
    differences, _ = _round_trip_anthropic(body)
    assert differences == ()


def test_anthropic_null_block_fields_echoed_by_the_sdk_are_ignored() -> None:
    # The Anthropic SDK echoes a response's text block back with `citations: null`; the API treats
    # the field as absent, and so does the comparison.
    raw: JSONObject = {
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "hi", "citations": None}]}
        ],
        "max_tokens": 5,
    }
    encoded: JSONObject = {
        "messages": [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}],
        "max_tokens": 5,
    }
    differences, ignored = compare_request("anthropic_messages", raw, encoded)
    assert differences == ()
    assert "null-valued block fields" in ignored


def test_responses_priority_is_the_same_tier_as_fast() -> None:
    differences, ignored = compare_request(
        "openai_responses",
        {"input": "hi", "service_tier": "priority"},
        {"input": "hi", "service_tier": "fast"},
    )
    assert differences == ()
    assert "service_tier priority-vs-fast" in ignored


def test_anthropic_stop_details_are_compared_when_a_refusal_carries_them() -> None:
    base: JSONObject = {
        "content": [],
        "stop_reason": "refusal",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    differences, ignored = compare_response(
        "anthropic_messages", {**base, "stop_details": None}, dict(base)
    )
    assert differences == ()
    assert "stop_details null-vs-absent" in ignored
    detail: JSONObject = {"type": "policy_violation", "policy": "cyber"}
    differences, _ = compare_response(
        "anthropic_messages", {**base, "stop_details": detail}, {**base, "stop_details": detail}
    )
    assert differences == ()
    differences, _ = compare_response(
        "anthropic_messages", {**base, "stop_details": detail}, dict(base)
    )
    assert [d.path for d in differences] == ["/stop_details"]


def test_responses_serving_metadata_phase_and_empty_content_are_ignored_with_reasons() -> None:
    # What gpt-5.6 returned on 2026-09-30 beyond what the vocabulary models.
    served: JSONObject = {
        "metadata": {"turn_id": "01a0f351"},
        "internal_chat_message_metadata_passthrough": {"turn_id": "01a0f351", "create_time": 1.5},
    }
    raw: JSONObject = {
        "status": "completed",
        "output": [
            {
                "id": "rs_1",
                "type": "reasoning",
                "summary": [],
                "content": [],
                "encrypted_content": "enc",
                **served,
            },
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "phase": "final_answer",
                "content": [
                    {"type": "output_text", "text": "hi", "annotations": [], "logprobs": []}
                ],
                **served,
            },
        ],
        "usage": {
            "input_tokens": 4016,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 4013},
            "output_tokens": 5,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 4021,
        },
    }
    encoded: JSONObject = {
        "status": "completed",
        "output": [
            {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "enc"},
            {
                "id": "msg_behaviors",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "hi", "annotations": []}],
            },
        ],
        "usage": raw["usage"],
    }
    differences, ignored = compare_response("openai_responses", raw, encoded)
    assert differences == ()
    assert {"message phase", "item serving metadata", "empty reasoning content"} <= set(ignored)
    # A non-empty reasoning content is a real difference, not a spelling.
    raw_output = cast("list[JSONObject]", raw["output"])
    raw_output[0]["content"] = [{"type": "reasoning_text", "text": "raw"}]
    differences, _ = compare_response("openai_responses", raw, encoded)
    assert [d.path for d in differences] == ["/output/0/content"]


def test_responses_response_projection_compares_items_without_generated_ids() -> None:
    raw: JSONObject = {
        "id": "resp_real",
        "status": "completed",
        "incomplete_details": None,
        "output": [
            {
                "id": "rs_1",
                "type": "reasoning",
                "summary": [],
                "encrypted_content": "e",
                "status": None,
            },
            {
                "id": "fc_real",
                "type": "function_call",
                "call_id": "call_1",
                "name": "run",
                "arguments": '{"x":1}',
                "status": "completed",
            },
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    encoded: JSONObject = {
        "id": "resp_synth",
        "status": "completed",
        "incomplete_details": None,
        "output": [
            {"id": "rs_1", "type": "reasoning", "summary": [], "encrypted_content": "e"},
            {
                "id": "fc_synth",
                "type": "function_call",
                "status": "completed",
                "arguments": '{"x": 1}',
                "call_id": "call_1",
                "name": "run",
            },
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    differences, _ = compare_response("openai_responses", raw, encoded)
    assert differences == ()
    # A changed tool-call id or reasoning id is never ignored.
    raw_items = cast("list[dict[str, Any]]", raw["output"])
    raw_items[1]["call_id"] = "call_other"
    differences, _ = compare_response("openai_responses", raw, encoded)
    assert [(d.path, d.kind) for d in differences] == [("/output/1/call_id", "value_differs")]


def test_responses_custom_tools_namespaces_and_additional_tools_round_trip() -> None:
    grammar = {"type": "grammar", "syntax": "lark", "definition": "start: /.*/"}
    body: dict[str, Any] = {
        "model": "gpt-harness",
        "stream": True,
        "input": [
            {
                "type": "additional_tools",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "functions",
                        "description": "Core tools.",
                        "tools": [
                            {"type": "custom", "name": "exec", "format": grammar},
                            {"type": "function", "name": "wait", "parameters": {"type": "object"}},
                        ],
                    }
                ],
            },
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {
                "type": "custom_tool_call",
                "call_id": "call_e",
                "name": "exec",
                "namespace": "functions",
                "input": "run()",
            },
            {"type": "custom_tool_call_output", "call_id": "call_e", "output": "done"},
            {
                "type": "function_call",
                "call_id": "call_w",
                "name": "wait",
                "namespace": "functions",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call_w", "output": "ok"},
        ],
        "tools": [{"type": "custom", "name": "apply_patch"}],
    }
    codec = OpenAIResponsesCodec()
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    encoded = codec.encode_request("gpt-subject", decoded.request, streaming=True)
    differences, ignored = compare_request(
        "openai_responses", cast("JSONObject", body), encoded.body
    )
    assert differences == ()
    assert {
        "additional_tools hoisted into tools",
        "namespace entries flattened",
        "custom tool default format",
    } <= set(ignored)


def test_responses_additions_to_a_declared_namespace_and_empty_additions_round_trip() -> None:
    # A member added to a group the top-level list already declares is regrouped with it on
    # the way back, and an addition that adds nothing leaves no trace; neither is a difference.
    body: dict[str, Any] = {
        "model": "gpt-harness",
        "stream": True,
        "input": [
            {"type": "additional_tools", "tools": []},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {
                "type": "additional_tools",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "ns",
                        "description": "Group.",
                        "tools": [
                            {"type": "function", "name": "late", "parameters": {"type": "object"}}
                        ],
                    }
                ],
            },
        ],
        "tools": [
            {
                "type": "namespace",
                "name": "ns",
                "description": "Group.",
                "tools": [{"type": "function", "name": "early", "parameters": {"type": "object"}}],
            },
            {"type": "function", "name": "plain", "parameters": {"type": "object"}},
        ],
    }
    codec = OpenAIResponsesCodec()
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    encoded = codec.encode_request("gpt-subject", decoded.request, streaming=True)
    differences, _ = compare_request("openai_responses", cast("JSONObject", body), encoded.body)
    assert differences == ()


def test_responses_a_dropped_namespace_description_is_a_difference() -> None:
    body: dict[str, Any] = {
        "model": "gpt-harness",
        "input": "go",
        "tools": [
            {
                "type": "namespace",
                "name": "ns",
                "description": "Read me.",
                "tools": [{"type": "function", "name": "t", "parameters": {"type": "object"}}],
            }
        ],
    }
    forgotten = json.loads(json.dumps(body))
    forgotten["tools"][0]["description"] = ""
    differences, _ = compare_request(
        "openai_responses", cast("JSONObject", body), cast("JSONObject", forgotten)
    )
    assert [(d.path, d.harness, d.translated) for d in differences] == [
        ("/tools/0/namespace_description", "Read me.", "")
    ]


def test_responses_system_prompt_as_a_leading_developer_item_round_trips() -> None:
    # Codex 0.160 sends its system prompt as the first `developer` input item rather than as
    # `instructions`; the encoder spells the first system message as `instructions`, which the
    # API defines as the same leading developer message.
    body: dict[str, Any] = {
        "model": "gpt-harness",
        "stream": True,
        "input": [
            {"role": "developer", "content": [{"type": "input_text", "text": "You are Codex."}]},
            {"role": "developer", "content": [{"type": "input_text", "text": "<permissions/>"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "go"}]},
        ],
        "tools": [{"type": "function", "name": "t", "parameters": {"type": "object"}}],
    }
    codec = OpenAIResponsesCodec()
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    assert [m.role for m in decoded.request.context] == ["system", "system", "user"]
    encoded = codec.encode_request("gpt-subject", decoded.request, streaming=True)
    assert encoded.body["instructions"] == "You are Codex."
    differences, ignored = compare_request(
        "openai_responses", cast("JSONObject", body), encoded.body
    )
    assert differences == ()
    assert "instructions as a leading developer item" in ignored


def test_responses_gpt6_echo_metadata_is_not_a_difference() -> None:
    # Current Codex echoes what the API stamped on its items: `phase` on assistant messages,
    # `status` on call items, ids on outputs, and no namespace on a code-mode `exec` call even
    # though `exec` is declared under `functions`. None of it is carried by the vocabulary,
    # and none of it is a reproduction failure.
    body: dict[str, Any] = {
        "model": "gpt-harness",
        "stream": True,
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "go"}]},
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Running it."}],
            },
            {
                "type": "custom_tool_call",
                "id": "ctc_1",
                "status": "completed",
                "call_id": "call_e",
                "name": "exec",
                "input": "text(1)",
            },
            {"type": "custom_tool_call_output", "id": "ctco_1", "call_id": "call_e", "output": "1"},
            {
                "type": "function_call",
                "id": "fc_1",
                "status": "completed",
                "call_id": "call_w",
                "name": "wait",
                "namespace": "functions",
                "arguments": '{"cell_id": "c"}',
            },
            {"type": "function_call_output", "id": "fco_1", "call_id": "call_w", "output": "ok"},
        ],
        "tools": [
            {
                "type": "namespace",
                "name": "functions",
                "description": "",
                "tools": [
                    {"type": "custom", "name": "exec", "format": {"type": "text"}},
                    {"type": "function", "name": "wait", "parameters": {"type": "object"}},
                ],
            }
        ],
    }
    codec = OpenAIResponsesCodec()
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    encoded = codec.encode_request("gpt-subject", decoded.request, streaming=True)
    differences, ignored = compare_request(
        "openai_responses", cast("JSONObject", body), encoded.body
    )
    assert differences == ()
    assert {"assistant message phase", "echoed item status"} <= set(ignored)


def test_anthropic_echoed_output_config_is_not_a_difference() -> None:
    # Claude Code echoes the `output_config` (effort) the API stamped on an assistant message;
    # nothing in the vocabulary carries it, and it is not a reproduction failure.
    body: dict[str, Any] = {
        "model": "claude-harness",
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "go"}]},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
                "output_config": {"effort": "medium"},
            },
            {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
        ],
    }
    codec = AnthropicMessagesCodec()
    decoded = codec.decode_request("/v1/messages", (), json.dumps(body).encode())
    encoded = codec.encode_request("claude-subject", decoded.request, streaming=True)
    differences, ignored = compare_request(
        "anthropic_messages", cast("JSONObject", body), encoded.body
    )
    assert differences == ()
    assert "assistant message output_config" in ignored


def test_summary_choice_crosses_dialects_and_reconstructs_in_place() -> None:
    # An Anthropic harness asking to omit summaries is served by a Responses subject with no
    # summary, and vice versa; within a dialect the request reconstructs as sent.
    anthropic = AnthropicMessagesCodec()
    responses = OpenAIResponsesCodec()
    omitted: dict[str, Any] = {
        "model": "claude-harness",
        "max_tokens": 64,
        "thinking": {"type": "adaptive", "display": "omitted"},
        "messages": [{"role": "user", "content": [{"type": "text", "text": "go"}]}],
    }
    decoded = anthropic.decode_request("/v1/messages", (), json.dumps(omitted).encode())
    assert decoded.request.sampling.reasoning == ReasoningParams(summary=False)
    to_responses = responses.encode_request("gpt-subject", decoded.request, streaming=True)
    assert to_responses.body["reasoning"] == {}
    same = anthropic.encode_request("claude-subject", decoded.request, streaming=True)
    differences, _ = compare_request("anthropic_messages", cast("JSONObject", omitted), same.body)
    assert differences == ()

    wanted: dict[str, Any] = {
        "model": "gpt-harness",
        "input": "go",
        "reasoning": {"effort": "medium", "summary": "detailed"},
    }
    decoded = responses.decode_request("/v1/responses", (), json.dumps(wanted).encode())
    to_anthropic = anthropic.encode_request("claude-subject", decoded.request, streaming=True)
    assert cast("JSONObject", to_anthropic.body["thinking"])["display"] == "summarized"
    same = responses.encode_request("gpt-subject", decoded.request, streaming=True)
    differences, _ = compare_request("openai_responses", cast("JSONObject", wanted), same.body)
    assert differences == ()


@pytest.mark.parametrize("spelling", ["between_tools", "disabled"])
def test_anthropic_thinking_off_spellings_are_one(spelling: str) -> None:
    # Both spell no thinking before the response; the codec renders the canonical `disabled`
    # whatever model the harness named, and the comparison treats the two as one leaf.
    body: dict[str, Any] = {
        "model": "claude-test",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "go"}]}],
        "thinking": {"type": spelling},
    }
    codec = AnthropicMessagesCodec()
    decoded = codec.decode_request("/v1/messages", (), json.dumps(body).encode())
    assert decoded.request.sampling.reasoning == ReasoningParams(enabled=False)
    encoded = codec.encode_request("claude-test", decoded.request, streaming=True)
    assert encoded.body["thinking"] == {"type": "disabled"}
    differences, ignored = compare_request(
        "anthropic_messages", cast("JSONObject", body), encoded.body
    )
    assert differences == ()
    assert ("thinking off spelling" in ignored) == (spelling == "between_tools")


def test_anthropic_tool_use_cache_breakpoint_round_trips() -> None:
    body: dict[str, Any] = {
        "model": "claude-harness",
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "go"}]},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "t",
                        "input": {"x": 1},
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}],
            },
        ],
        "tools": [{"name": "t", "input_schema": {"type": "object"}}],
    }
    codec = AnthropicMessagesCodec()
    decoded = codec.decode_request("/v1/messages", (), json.dumps(body).encode())
    encoded = codec.encode_request("claude-subject", decoded.request, streaming=True)
    differences, _ = compare_request("anthropic_messages", cast("JSONObject", body), encoded.body)
    assert differences == ()


def test_anthropic_tool_streaming_hint_is_not_a_difference() -> None:
    body: dict[str, Any] = {
        "model": "claude-harness",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "go"}]}],
        "tools": [
            {
                "name": "t",
                "input_schema": {"type": "object"},
                "eager_input_streaming": True,
            }
        ],
    }
    codec = AnthropicMessagesCodec()
    decoded = codec.decode_request("/v1/messages", (), json.dumps(body).encode())
    encoded = codec.encode_request("claude-subject", decoded.request, streaming=True)
    differences, ignored = compare_request(
        "anthropic_messages", cast("JSONObject", body), encoded.body
    )
    assert differences == ()
    assert "tool eager_input_streaming hint" in ignored


def test_a_gemini_legacy_schema_is_its_json_schema() -> None:
    # Gemini CLI declares some tools with the older `parameters` Schema (UPPERCASE types); the
    # vocabulary keeps JSON Schema, which the codec renders as `parametersJsonSchema`.
    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "run",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {"command": {"type": "STRING"}},
                        },
                    }
                ]
            }
        ],
        "generationConfig": {"stopSequences": []},
    }
    codec = GeminiGenerateContentCodec()
    target = "/v1beta/models/gemini-test:streamGenerateContent?alt=sse"
    decoded = codec.decode_request(target, (), json.dumps(body).encode())
    encoded = codec.encode_request("gemini-test", decoded.request, streaming=True)
    differences, ignored = compare_request(
        "gemini_generate_content", cast("JSONObject", body), encoded.body
    )
    assert differences == ()
    assert "legacy parameters schema as JSON Schema" in ignored
    assert "empty stop sequences" in ignored
