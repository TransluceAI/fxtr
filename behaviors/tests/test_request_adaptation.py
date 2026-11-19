"""Every change `adapt_request` makes is recorded, for every codec, and its result encodes.

A set of probe requests, each carrying one thing some dialect cannot carry as stated, adapted by
each model-side codec (and each chat completions configuration). For each: the encoder takes the
adapted request; every part of the request adaptation changed is named by a recorded loss of the
matching kind; every recorded loss names a part that changed; and a loss key carries the same
class (semantic or not) wherever it is recorded.
"""

import dataclasses
import json
from collections.abc import Callable
from typing import Any, cast

import pytest

from behaviors.implementations.dialects.anthropic_messages import (
    AnthropicMessagesCodec,
    AnthropicRedactedThinking,
    AnthropicSignature,
)
from behaviors.implementations.dialects.gemini_generate_content import (
    GeminiGenerateContentCodec,
    GeminiThoughtSignature,
)
from behaviors.implementations.dialects.openai_chat_completions import (
    OpenAIChatCompletionsCodec,
    OpenRouterReasoning,
)
from behaviors.implementations.dialects.openai_responses import (
    OpenAIResponsesCodec,
    OpenAIResponsesReasoning,
)
from behaviors.interfaces.dialect_codecs import ModelSideCodec
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import (
    CacheableContentText,
    CacheableContentToolUse,
    Content,
    ContentReasoning,
    ContentText,
    ContentToolUse,
    ReasoningForm,
)
from behaviors.types.dialect import DialectExtras
from behaviors.types.message import (
    AssistantMessage,
    CacheableToolMessage,
    ChatMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from behaviors.types.response import ModelRequest, ReasoningParams, SamplingParams
from behaviors.types.tool import CacheableToolParam, ToolParam
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData

_MODEL = "test-model"

_CODECS: dict[str, ModelSideCodec] = {
    "anthropic": AnthropicMessagesCodec(),
    "responses": OpenAIResponsesCodec(),
    "chat completions": OpenAIChatCompletionsCodec(),
    "chat completions, no echo field": OpenAIChatCompletionsCodec(reasoning_echo_field=None),
    "openrouter, forwarding breakpoints": OpenAIChatCompletionsCodec(
        variant="openrouter", forward_cache_control=True
    ),
    "gemini": GeminiGenerateContentCodec(),
}

_HI = UserMessage(content=(ContentText(text="hi"),))
_CALL = ContentToolUse(id="c1", name="run", input={}, parse_error=None)
_RESULT = ToolMessage(content=(ContentText(text="ok"),), tool_call_id="c1", is_error=None)
_TOOL = ToolParam(name="run", description=None, input_schema={}, strict=False, input_examples=None)
_MARK = CacheControl()


def _request(
    *context: ChatMessage, tools: tuple[ToolParam, ...] = (), **sampling: Any
) -> ModelRequest:
    return ModelRequest(
        context=(_HI, *context)
        if not context or not isinstance(context[0], SystemMessage)
        else context,
        tools=tools,
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(**sampling),
    )


def _thought(block: Content) -> ModelRequest:
    return _request(AssistantMessage(content=(block, ContentText(text="answer"))), _HI)


def _stated(text: str, form: ReasoningForm, state: TaggedDataclass) -> ContentReasoning:
    return ContentReasoning(text=text, form=form, reasoning_state=DynamicData.from_tagged(state))


def _marked(cacheable: Any, value: Any) -> Any:
    """`value` as its `cacheable` variant, with a breakpoint."""
    fields = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
    return cacheable(**fields, cache_control=_MARK)


_PROBES: dict[str, ModelRequest] = {
    "literal reasoning": _thought(ContentReasoning(text="hm", form="full")),
    "unsigned summary": _thought(ContentReasoning(text="hm", form="summary")),
    "redaction without state": _thought(ContentReasoning(text="", form="redacted")),
    "anthropic redacted thinking": _thought(
        _stated("", "redacted", AnthropicRedactedThinking(data="blob"))
    ),
    "anthropic signature": _thought(_stated("hm", "summary", AnthropicSignature(signature="Eq"))),
    "responses state": _thought(
        _stated("hm", "summary", OpenAIResponsesReasoning(id="rs_1", encrypted_content="enc"))
    ),
    "gemini signature": _thought(
        _stated("hm", "summary", GeminiThoughtSignature(model="gemini-x", signature="s", on="next"))
    ),
    "another model's openrouter state": _thought(
        _stated("hm", "summary", OpenRouterReasoning(model="other/model", details=({"type": "x"},)))
    ),
    "stop sequences": _request(stop_sequences=("STOP",)),
    "effort minimal": _request(reasoning=ReasoningParams(effort="minimal")),
    "effort max": _request(reasoning=ReasoningParams(effort="max")),
    "budget only": _request(reasoning=ReasoningParams(budget_tokens=4096)),
    "speed": _request(speed="fast"),
    "tool error flag": _request(
        AssistantMessage(content=(_CALL,)), dataclasses.replace(_RESULT, is_error=True)
    ),
    "result after text": _request(AssistantMessage(content=(_CALL,)), _HI, _RESULT),
    "later system message": _request(
        AssistantMessage(content=(ContentText(text="a"),)),
        SystemMessage(content=(ContentText(text="be brief"),)),
        _HI,
    ),
    "breakpoint on user text": _request(
        UserMessage(content=(CacheableContentText(text="q", cache_control=_MARK),))
    ),
    "breakpoint on empty text": _request(
        UserMessage(content=(CacheableContentText(text="", cache_control=_MARK),))
    ),
    "breakpoint on a tool": _request(tools=(_marked(CacheableToolParam, _TOOL),)),
    "breakpoint on a tool call": _request(
        AssistantMessage(content=(_marked(CacheableContentToolUse, _CALL),)),
        _RESULT,
    ),
    "breakpoint on a tool result": _request(
        AssistantMessage(content=(_CALL,)),
        _marked(CacheableToolMessage, _RESULT),
    ),
    "five breakpoints": _request(
        *(
            UserMessage(content=(CacheableContentText(text=f"q{i}", cache_control=_MARK),))
            for i in range(5)
        )
    ),
    "a longer TTL after a shorter one": _request(
        UserMessage(content=(CacheableContentText(text="a", cache_control=CacheControl("5m")),)),
        UserMessage(content=(CacheableContentText(text="b", cache_control=CacheControl("1h")),)),
    ),
    "another dialect's extras": dataclasses.replace(
        _request(), extras=DialectExtras(dialect="openai_responses", body={"store": True})
    ),
}

# What changed, by part of the request, and the loss keys that may name each change.
_ASPECTS: dict[str, tuple[Callable[[ModelRequest], object], frozenset[str]]] = {
    "reasoning blocks": (
        lambda r: [
            [b for b in m.content if not isinstance(b, ContentText | ContentToolUse)]
            for m in r.context
            if isinstance(m, AssistantMessage)
        ],
        frozenset(
            {"literal_reasoning", "unsigned_reasoning", "redacted_reasoning", "foreign_reasoning"}
        ),
    ),
    "message order and roles": (
        lambda r: [(m.role, getattr(m, "tool_call_id", None)) for m in r.context],
        frozenset({"tool_result_order", "system_role"}),
    ),
    "tool error flags": (
        lambda r: [m.is_error for m in r.context if isinstance(m, ToolMessage)],
        frozenset({"tool_error_flag"}),
    ),
    "cache breakpoints": (
        lambda r: [
            c
            for v in (*r.tools, *r.context, *(b for m in r.context for b in m.content))
            if (c := getattr(v, "cache_control", None)) is not None
        ],
        frozenset({"cache_breakpoint"}),
    ),
    "stop sequences": (lambda r: r.sampling.stop_sequences, frozenset({"stop_sequences"})),
    "reasoning settings": (
        lambda r: r.sampling.reasoning,
        frozenset({"reasoning_effort", "reasoning_budget"}),
    ),
    "speed": (lambda r: r.sampling.speed, frozenset({"speed"})),
    "extras": (lambda r: r.extras, frozenset({"extras"})),
}


# Losses for what a dialect shows the model differently from an unchanged request.
_RENDERED = frozenset({"tool_name", "freeform_format"})


@pytest.mark.parametrize("codec_name", sorted(_CODECS))
def test_every_change_adaptation_makes_is_recorded(codec_name: str) -> None:
    codec = _CODECS[codec_name]
    problems: list[str] = []
    for probe_name, request in _PROBES.items():
        adapted = codec.adapt_request(_MODEL, request)
        codec.encode_request(_MODEL, adapted.request, streaming=True)
        keys = {loss.code for loss in adapted.losses}
        named: set[str] = set()
        for aspect, (read, allowed) in _ASPECTS.items():
            if read(request) != read(adapted.request):
                if not keys & allowed:
                    problems.append(f"{probe_name}: {aspect} changed, with no loss naming it")
                named |= keys & allowed
        # These name how the encoder renders an unchanged request (tested on their own below).
        if keys - named - _RENDERED:
            problems.append(f"{probe_name}: {sorted(keys - named)} recorded, with nothing changed")
    assert not problems, "\n".join(problems)


def test_a_loss_key_always_has_the_same_class() -> None:
    classes: dict[str, set[bool]] = {}
    for codec in _CODECS.values():
        for request in _PROBES.values():
            for loss in codec.adapt_request(_MODEL, request).losses:
                classes.setdefault(loss.code, set()).add(loss.semantic)
    assert classes["cache_breakpoint"] == {False}
    assert {key: kinds for key, kinds in classes.items() if len(kinds) > 1} == {}


@pytest.mark.parametrize(
    ("codec_name", "ttl", "sent"),
    [
        ("anthropic", None, None),
        ("anthropic", "1h", "1h"),
        ("anthropic", "30m", "1h"),
        ("anthropic", "12h", "1h"),
        ("anthropic", "a while", None),
        ("openrouter, forwarding breakpoints", "12h", "1h"),
        ("responses", "5m", "30m"),
        ("responses", "1h", "12h"),
        ("responses", "24h", "12h"),
    ],
)
def test_cache_lifetimes_are_fitted_to_the_destination(
    codec_name: str, ttl: str | None, sent: str | None
) -> None:
    codec = _CODECS[codec_name]
    request = _request(
        UserMessage(content=(CacheableContentText("hi", cache_control=CacheControl(ttl)),))
    )
    adapted = codec.adapt_request(_MODEL, request)
    assert [(loss.code, loss.semantic) for loss in adapted.losses] == (
        [] if sent == ttl else [("cache_breakpoint", False)]
    )
    body = codec.encode_request(_MODEL, adapted.request, streaming=True).body
    items = cast("list[dict[str, Any]]", body["input" if codec_name == "responses" else "messages"])
    key = "prompt_cache_breakpoint" if codec_name == "responses" else "cache_control"
    assert items[-1]["content"][-1][key].get("ttl") == sent


def _effective_anthropic_ttls(body: dict[str, Any]) -> list[str]:
    """Each sent breakpoint's lifetime in render order, with the API's 5-minute default where a
    breakpoint has no TTL."""
    ttls: list[str] = []

    def visit(block: dict[str, Any]) -> None:
        if isinstance(block.get("content"), list):
            for inner in block["content"]:
                visit(inner)
        if "cache_control" in block:
            ttls.append(block["cache_control"].get("ttl", "5m"))

    for message in body["messages"]:
        if isinstance(message["content"], list):
            for block in message["content"]:
                visit(block)
    return ttls


@pytest.mark.parametrize(
    ("codec_name", "context", "sent", "changed"),
    [
        pytest.param(
            "anthropic",
            (
                UserMessage(content=(CacheableContentText("a", cache_control=CacheControl("5m")),)),
                UserMessage(
                    content=(CacheableContentText("b", cache_control=CacheControl("12h")),)
                ),
            ),
            ["5m", "5m"],
            True,
            id="anthropic: fitted, then ordered",
        ),
        pytest.param(
            "openrouter, forwarding breakpoints",
            (
                SystemMessage(
                    content=(CacheableContentText("s", cache_control=CacheControl("5m")),)
                ),
                UserMessage(
                    content=(CacheableContentText("u", cache_control=CacheControl("30m")),)
                ),
            ),
            ["5m", "5m"],
            True,
            id="openrouter: fitted, then ordered",
        ),
        pytest.param(
            "anthropic",
            (
                CacheableToolMessage(
                    content=(CacheableContentText("r", cache_control=CacheControl("30m")),),
                    tool_call_id="c",
                    is_error=None,
                    cache_control=CacheControl(),
                ),
            ),
            ["1h", "1h"],
            True,
            id="anthropic: a tool result and its final block share the longer lifetime",
        ),
        pytest.param(
            "anthropic",
            (
                CacheableToolMessage(
                    content=(
                        CacheableContentText("a", cache_control=CacheControl("5m")),
                        ContentText("b"),
                    ),
                    tool_call_id="c",
                    is_error=None,
                    cache_control=CacheControl("1h"),
                ),
            ),
            ["5m", "5m"],
            True,
            id="anthropic: a tool result's content comes before its own breakpoint",
        ),
        pytest.param(
            "anthropic",
            (
                CacheableToolMessage(
                    content=(
                        CacheableContentText("a", cache_control=CacheControl("1h")),
                        ContentText("b"),
                    ),
                    tool_call_id="c",
                    is_error=None,
                    cache_control=CacheControl("5m"),
                ),
            ),
            ["1h", "5m"],
            False,
            id="anthropic: a longer TTL inside a tool result is already in order",
        ),
    ],
)
def test_anthropic_style_breakpoints_are_sent_in_ttl_order(
    codec_name: str, context: tuple[ChatMessage, ...], sent: list[str], changed: bool
) -> None:
    codec = _CODECS[codec_name]
    adapted = codec.adapt_request(_MODEL, _request(*context))
    cache_losses = [loss for loss in adapted.losses if loss.code == "cache_breakpoint"]
    assert [loss.semantic for loss in cache_losses] == ([False] if changed else [])
    body = codec.encode_request(_MODEL, adapted.request, streaming=True).body
    assert _effective_anthropic_ttls(cast("dict[str, Any]", body)) == sent


@pytest.mark.parametrize("codec_name", sorted(_CODECS))
def test_a_dialect_without_namespaces_or_grammars_records_what_the_model_sees(
    codec_name: str,
) -> None:
    def tool(name: str, namespace: str | None = None, **fields: Any) -> ToolParam:
        return dataclasses.replace(_TOOL, name=name, namespace=namespace, **fields)

    request = _request(
        tools=(
            tool("shell", "local"),
            tool("shell", "remote"),
            tool("patch", freeform_format={"type": "grammar", "syntax": "lark", "definition": "x"}),
        )
    )
    keys = {loss.code for loss in _CODECS[codec_name].adapt_request(_MODEL, request).losses}
    expected = set() if codec_name == "responses" else {"tool_name", "freeform_format"}
    assert keys & _RENDERED == expected


# The dialects with a harness side, which can read a request back from the wire.
_READ_BACK = {
    "anthropic": "/v1/messages",
    "responses": "/v1/responses",
    "gemini": "/v1beta/models/test-model:streamGenerateContent?alt=sse",
}


def _as_read_back(aspect: str, value: Any) -> Any:
    """`value` of `aspect` in the spelling a read-back compares: consecutive messages of one
    role are one turn (Gemini merges them), and an unset summary choice is the encoder's
    declared default (Responses asks for summaries)."""
    if aspect == "message order and roles":
        merged: list[Any] = []
        for entry in value:
            if not merged or merged[-1][0] != entry[0] or entry[1] is not None:
                merged.append(entry)
        return merged
    if aspect == "reasoning settings" and value is not None:
        return dataclasses.replace(value, summary=None)
    return value


@pytest.mark.parametrize("codec_name", sorted(_READ_BACK))
def test_the_wire_carries_what_adaptation_left(codec_name: str) -> None:
    # Read back from the wire, a request means what the adapted request says: with the test
    # above (every change from the request to the adapted one is recorded), nothing reaches the
    # provider changed without a recorded loss.
    codec = _CODECS[codec_name]
    problems: list[str] = []
    for probe_name, request in _PROBES.items():
        adapted = codec.adapt_request(_MODEL, request).request
        encoded = codec.encode_request(_MODEL, adapted, streaming=True)
        decoded = codec.decode_request(  # type: ignore[attr-defined]
            _READ_BACK[codec_name], encoded.headers, json.dumps(encoded.body).encode()
        )
        read = decoded.request
        unread = {loss.code for loss in decoded.decode_losses}
        for aspect, (reader, keys) in _ASPECTS.items():
            # A dialect's extras are its own declared defaults; and what the decoder itself
            # cannot hold (a literal reasoning item read back by Responses) it cannot vouch for.
            if aspect == "extras" or unread & keys:
                continue
            sent = _as_read_back(aspect, reader(adapted))
            if _as_read_back(aspect, reader(read)) != sent:
                problems.append(f"{probe_name}: {aspect} differs on the wire")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("state_for", "accepted_by"),
    [
        (
            lambda model: OpenRouterReasoning(model=model, details=({"type": "x"},)),
            "openrouter, forwarding breakpoints",
        ),
        (lambda model: GeminiThoughtSignature(model=model, signature="s", on="next"), "gemini"),
    ],
)
def test_reasoning_state_naming_its_model_goes_only_to_that_model(
    state_for: Callable[[str], TaggedDataclass], accepted_by: str
) -> None:
    # A state that names the model it was produced for is kept only by the codec it belongs to,
    # and only for that model; every other codec, and that codec for another model, leaves it out
    # (`drop_foreign_reasoning`) and records it.
    def history(producer: str) -> ModelRequest:
        return _thought(_stated("hm", "summary", state_for(producer)))

    for name, codec in _CODECS.items():
        own = [loss.code for loss in codec.adapt_request(_MODEL, history(_MODEL)).losses]
        other = [loss.code for loss in codec.adapt_request(_MODEL, history("another")).losses]
        if name == accepted_by:
            assert own == [], name
            assert other == ["foreign_reasoning"], name
        else:
            assert "foreign_reasoning" in own and "foreign_reasoning" in other, name


@pytest.mark.parametrize(
    ("form", "code"),
    [
        ("full", "literal_reasoning"),
        ("summary", "unsigned_reasoning"),
        ("redacted", "redacted_reasoning"),
    ],
)
def test_reasoning_without_state_is_recorded_by_its_form(form: ReasoningForm, code: str) -> None:
    text = "" if form == "redacted" else "hm"
    request = _thought(ContentReasoning(text=text, form=form))
    adapted = _CODECS["anthropic"].adapt_request(_MODEL, request)
    assert [loss.code for loss in adapted.losses] == [code]
