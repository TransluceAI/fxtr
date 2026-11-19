"""Anthropic `/v1/messages` ⇄ behaviors model vocabulary translation: the `AnthropicMessagesCodec`.

Pure functions and a codec object binding them to the `DialectCodec` protocol; no I/O and no
third-party imports. The harness side (serving a harness that speaks this dialect) is documented
here; the model side (`AnthropicMessagesAPI` calling a provider that
speaks it: `encode_anthropic_request`, `AnthropicSseDecoder`, `decode_anthropic_message`,
`decode_anthropic_failure`) is documented at its own section further down. Harness side:

* **Request** (`anthropic_request_to_canonical`): an Anthropic Messages request body → a
  `DecodedRequest`. Anthropic's top-level `system` prompt is folded into a leading
  `SystemMessage`, and a `system`-role message inside `messages` (a mid-conversation system
  message; Claude Code sends its agent-type reminder this way) into a `SystemMessage` at its
  position; each `tool_result` block becomes its own `ToolMessage`; `thinking` and
  `redacted_thinking` blocks map to `ContentReasoning`. A `thinking` block's signature is either
  Anthropic's own (an `AnthropicSignature` state) or an envelope this codec wrote around another
  form or state (`_reasoning_envelope`), so reasoning of any provider round-trips through the
  harness.
* **Response**: `synthesize_anthropic_sse` turns a subject-model stream (`StreamEvent` deltas plus
  the terminal `ModelResponse` or `ModelCallFailure`) into the Anthropic streaming SSE event
  sequence (`message_start` → per-block `content_block_*` → `message_delta` → `message_stop`);
  `response_to_anthropic_message` builds the non-streaming `Message` body from an assembled
  response. `anthropic_failure_body` renders a request-time `ModelCallFailure` as the dialect's
  status + error body — `overloaded` keeps Anthropic's 529 `overloaded_error` so harness backoff
  keeps triggering.

Request coverage: what the decoder does with each field the request schema allows (models it, keeps
it as an extra for a same-dialect subject, or drops it with a recorded loss), and how the
reconstruction check compares it, is tabulated per schema path in
`coverage/anthropic_messages.yaml`. The table is generated from the pinned SDK's request schema by
`tests/dialect_coverage`, and a test fails when the codec stops matching it. The notes below cover
what it cannot show: the response side, and the design choices behind some of its rows.

Known fidelity weak spots:

* **`input_tokens` at `message_start` is 0.** Usage arrives assembled on the terminal
  `ModelResponse`, so the streamed `message_start` cannot carry it; the real counts ride the final
  `message_delta`, and the `ModelResponse` keeps them.
* **Every reasoning block reaches the harness as a `thinking` block whose signature carries its
  state.** Anthropic's own signature is written raw, so the harness can resume directly against
  Anthropic; any other state, and reasoning with no state at all, is written as an envelope, which
  the harness echoes back like a signature. The text streams as it arrives and the signature is
  written when the block closes. Anthropic's redacted thinking has no text and is written whole as a
  `redacted_thinking` block once its state arrives.
* **Thinking off has two spellings.** `ReasoningParams(enabled=False)` renders as
  `thinking.type: disabled`, the canonical spelling; `between_tools`, which some models demand
  instead (they write short thinking blocks between tool calls but none before the response),
  decodes to the same neutral value. The codec knows no model: a model that wants the other
  spelling says so in its 400, and `AnthropicMessagesAPI` resends with it. The reconstruction
  check treats the two spellings as one (`thinking off spelling`). A model with no off switch
  recommends `adaptive` instead, which stays the error it is.
* **`cache_control` breakpoints survive request translation** (as `CacheableContentText` /
  `CacheableContentToolUse` / `CacheableToolParam` / `CacheableToolMessage`); whether they reach
  the provider is up to the subject model.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from behaviors.implementations.dialects._common import (
    BodyReader,
    LossLog,
    ReasoningStateFilter,
    ToolNames,
    apply_declared_defaults,
    apply_dialect_extras,
    failure_message,
    flattened_description,
    has_header,
    merge_provider_params,
    normalized_path,
    path_matches,
    request_extras,
    unadapted,
    without_reasoning,
)
from behaviors.implementations.dialects._reasoning_envelope import (
    envelope_text,
    read_text_slot,
)
from behaviors.implementations.dialects.adaptation import (
    AdaptedRequestBuilder,
    breakpoint_of,
    drop_breakpoints,
    drop_foreign_extras,
    drop_foreign_reasoning,
    drop_stateless_reasoning,
    fit_cache_ttls,
    merge_tool_result_breakpoints,
    note_flattened_tools,
    order_cache_ttls,
    replace_reasoning,
    tool_results_first,
)
from behaviors.implementations.dialects.codec_utils import (
    SseFrameParser,
    decode_json_object,
    own_reasoning_state,
)
from behaviors.implementations.dialects.contract_adaptation import (
    drop_json_mode,
    drop_output_schemas,
    enforce_response_format,
    fit_response_schema_to_anthropic,
    resolve_automatic_strictness,
    strict_problems,
    unenforce_incompatible_strict_tools,
)
from behaviors.interfaces.dialect_codecs import (
    AdaptedRequest,
    DecodedRequest,
    Route,
    SseDecoder,
    StopRendering,
)
from behaviors.interfaces.model_apis import ModelError
from behaviors.types.adaptation import ResponseLoss
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
from behaviors.types.dialect import Dialect, EncodedRequest, Headers
from behaviors.types.failure import FailureCategory, ModelCallFailure
from behaviors.types.json import JSONObject
from behaviors.types.message import (
    AssistantMessage,
    CacheableToolMessage,
    ChatMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from behaviors.types.namespace import behaviors_type
from behaviors.types.provider_tool import ProviderTool, ProviderToolFamily
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ReasoningEffort,
    ReasoningParams,
    ResponseFormat,
    ResponseFormatJsonObject,
    ResponseFormatJsonSchema,
    SamplingParams,
    SpeedTier,
    StopContentFilter,
    StopEndTurn,
    StopMaxTokens,
    StopOther,
    StopReason,
    StopRefusal,
    StopStopSequence,
    StopToolUse,
    Usage,
)
from behaviors.types.streaming import (
    StreamEvent,
    StreamReasoningDelta,
    StreamReasoningStart,
    StreamReasoningState,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
    SubjectStreamItem,
)
from behaviors.types.tool import (
    CacheableToolParam,
    ToolChoice,
    ToolChoiceAuto,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolChoiceTool,
    ToolParam,
)
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData


@dataclass(frozen=True)
class AnthropicSignature(TaggedDataclass, fxtr_type=behaviors_type("AnthropicSignature.v1")):
    """The `signature` of an Anthropic `thinking` block, which the API verifies against the
    block's text.

    Attributes:
        signature: The signature, as the API issued it.
    """

    signature: str


@dataclass(frozen=True)
class AnthropicRedactedThinking(
    TaggedDataclass, fxtr_type=behaviors_type("AnthropicRedactedThinking.v1")
):
    """The `data` of an Anthropic `redacted_thinking` block: the encrypted reasoning, sent back
    as is.

    Attributes:
        data: The encrypted reasoning, as the API issued it.
    """

    data: str


logger = logging.getLogger(__name__)

# --- request: Anthropic body -> DecodedRequest ---------------------------------------------------


def anthropic_request_to_canonical(
    body: dict[str, Any], headers: Headers = (), target: str = ""
) -> DecodedRequest:
    """Translate a parsed Anthropic `/v1/messages` request into a `DecodedRequest`.

    What the vocabulary has no slot for is attached as `DialectExtras` (`ModelRequest.extras`):
    the leftover leaves of `thinking` and `output_config` (beyond `effort` and a JSON-schema
    `format`), every top-level key the decoder does
    not read (`metadata`, `context_management`, `service_tier`, ...), feature-flag and unknown
    headers (`anthropic-beta`; `anthropic-version` is the subject client's own), and the query.
    """
    losses = LossLog()
    for server in _dict_list(body.get("mcp_servers")):
        # A hosted MCP connector is a provider-side tool; the key stays in the extras.
        losses.note(
            "provider_tool:mcp", f"hosted MCP connector mcp_servers:{server.get('name', '?')}"
        )
    reader = BodyReader(body)
    context = _convert_messages(reader.take("messages"), reader.take("system"), losses)
    tools = _convert_tools(reader.take("tools"), losses)
    tool_choice, parallel_tool_calls = _convert_tool_choice(reader.take("tool_choice"))
    sampling = SamplingParams(
        max_tokens=_as_int(reader.take("max_tokens")),
        temperature=_as_float(reader.take("temperature")),
        top_p=_as_float(reader.take("top_p")),
        stop_sequences=_str_list(reader.take("stop_sequences")),
        reasoning=_convert_reasoning(reader),
        speed=_convert_speed(reader),
    )
    model = reader.take("model")
    streaming = bool(reader.take("stream") or False)
    request = ModelRequest(
        context=tuple(context),
        tools=tuple(tools),
        tool_choice=tool_choice,
        parallel_tool_calls=parallel_tool_calls,
        sampling=sampling,
        response_format=_convert_output_format(reader),
        extras=request_extras(
            "anthropic_messages",
            reader,
            headers,
            target,
            contract_headers=("anthropic-version",),
        ),
    )
    return DecodedRequest(
        model=str(model if model is not None else ""),
        request=request,
        streaming=streaming,
        decode_losses=losses.recorded(),
    )


# Tool `type`s the API defines but the harness runs (the client executes the call and returns
# the result): declared without a schema, so the vocabulary has no `ToolParam` for them, but
# not provider-side either. `tool_search_tool_*` runs at the provider, over the request's own
# tools, with no reach beyond it.
_CLIENT_TOOL_TYPE_PREFIXES = (
    "bash_",
    "text_editor_",
    "computer_",
    "memory_",
    "str_replace_",
    "tool_search_tool_",
)

# Content blocks that are a provider-side tool's call or result, echoed back in the history.
_PROVIDER_TOOL_BLOCK_TYPES = frozenset(
    {
        "server_tool_use",
        "web_search_tool_result",
        "web_fetch_tool_result",
        "code_execution_tool_result",
        "bash_code_execution_tool_result",
        "text_editor_code_execution_tool_result",
        "mcp_tool_use",
        "mcp_tool_result",
        "container_upload",
    }
)


def _provider_tool_family(tool: dict[str, Any]) -> ProviderToolFamily | None:
    """The family of a `tools[]` entry, or None for a tool the harness runs."""
    kind = tool.get("type")
    if not isinstance(kind, str) or kind == "custom":
        return None  # a harness-run tool with its own schema (`type` is optional for those)
    if kind.startswith("web_search"):
        return "web_search"
    if kind.startswith("web_fetch"):
        return "web_fetch"
    if kind.startswith("code_execution"):
        return "code_execution"
    if kind.startswith("mcp_toolset"):
        return "mcp"
    if kind.startswith(_CLIENT_TOOL_TYPE_PREFIXES):
        return None
    return "unknown"


def provider_tools_in_request(body: dict[str, Any]) -> tuple[ProviderTool, ...]:
    """The provider-side tools a `/v1/messages` body declares: `tools[]` entries whose `type`
    names one (see `_provider_tool_family`) and every `mcp_servers` connector."""
    out: list[ProviderTool] = []
    for tool in _dict_list(body.get("tools")):
        family = _provider_tool_family(tool)
        if family is not None:
            out.append(ProviderTool(family=family, name=str(tool.get("type"))))
    for server in _dict_list(body.get("mcp_servers")):
        out.append(ProviderTool(family="mcp", name=f"mcp_servers:{server.get('name', '?')}"))
    return tuple(out)


_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "max")


def _convert_reasoning(reader: BodyReader) -> ReasoningParams | None:
    """The request's `thinking` config (plus `output_config.effort`) as neutral reasoning params.

    Anthropic's `thinking` shapes are the bijection `ReasoningParams` was designed around:
    off ↔ `enabled=False` (spelled `disabled`, or `between_tools` on the models that take that
    spelling; both mean no thinking before the response), enabled-with-budget ↔
    `budget_tokens`, adaptive ↔ neither, and
    `thinking.display` ↔ `summary` (`summarized` ↔ True, `omitted` ↔ False). Only the leaves
    that map are consumed: an unknown `thinking.type`, a non-integer budget, an unknown
    `display` value, an `output_config.effort` without any `thinking` config (no neutral slot)
    or with thinking disabled, and an unknown effort level all stay in the request's extras.
    """
    kind = reader.peek("thinking", "type")
    if kind in ("disabled", "between_tools"):
        reader.take_leaf("thinking", "type")
        return ReasoningParams(enabled=False)
    if kind == "enabled":
        reader.take_leaf("thinking", "type")
        budget = reader.peek("thinking", "budget_tokens")
        if _as_int(budget) is not None:
            reader.take_leaf("thinking", "budget_tokens")
        return ReasoningParams(
            budget_tokens=_as_int(budget),
            effort=_take_effort(reader),
            summary=_take_summary(reader),
        )
    if kind == "adaptive":
        reader.take_leaf("thinking", "type")
        return ReasoningParams(effort=_take_effort(reader), summary=_take_summary(reader))
    return None


_DISPLAY_OF_SUMMARY = {True: "summarized", False: "omitted"}


def _take_summary(reader: BodyReader) -> bool | None:
    """`thinking.display` as the neutral summary choice, consumed when it is one of the two
    values the vocabulary carries; any other value stays in the extras for the provider."""
    display = reader.peek("thinking", "display")
    for choice, value in _DISPLAY_OF_SUMMARY.items():
        if display == value:
            reader.take_leaf("thinking", "display")
            return choice
    return None


def _convert_speed(reader: BodyReader) -> SpeedTier | None:
    """The request's `speed` (fast mode) as a neutral tier; any other value stays in the extras
    for the provider to validate."""
    speed = _speed_tier(reader.peek("speed"))
    if speed is not None:
        reader.take("speed")
    return speed


def _speed_tier(value: object) -> SpeedTier | None:
    if value == "fast":
        return "fast"
    if value == "standard":
        return "standard"
    return None


def _convert_output_format(reader: BodyReader) -> ResponseFormatJsonSchema | None:
    """`output_config.format` as the response format. The API enforces every format it accepts,
    so the format is strict; one of a type other than `json_schema` stays in the extras."""
    fmt = _as_dict(reader.peek("output_config", "format"))
    if fmt is None or fmt.get("type") != "json_schema":
        return None
    schema = _as_dict(fmt.get("schema"))
    if schema is None or set(fmt) - {"type", "schema"}:
        return None
    reader.take_leaf("output_config", "format")
    return ResponseFormatJsonSchema(schema=cast("JSONObject", schema), strict=True)


def _take_effort(reader: BodyReader) -> ReasoningEffort | None:
    effort = reader.peek("output_config", "effort")
    if effort not in _EFFORT_LEVELS:
        return None
    reader.take_leaf("output_config", "effort")
    return cast("ReasoningEffort", effort)


def _as_dict(value: Any) -> dict[str, Any] | None:
    """A parsed JSON value as a str-keyed dict, or None.

    Isolates the JSON `Any` boundary so the rest of the module stays strictly typed: `isinstance`
    against `Any` otherwise narrows to `dict[Unknown]`.
    """
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _dict_list(value: Any) -> list[dict[str, Any]]:
    """The dict elements of a JSON list value (non-dicts and non-lists are skipped)."""
    if not isinstance(value, list):
        return []
    return [d for d in (_as_dict(item) for item in cast("list[Any]", value)) if d is not None]


def _str_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in cast("list[Any]", value))


def _convert_messages(messages: Any, system: Any, losses: LossLog) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    system_parts = _system_parts(system, losses)
    if system_parts:
        out.append(SystemMessage(content=system_parts))
    entries = _dict_list(messages)
    user_turns = [i for i, m in enumerate(entries) if m.get("role") not in ("assistant", "system")]
    for index, message in enumerate(entries):
        content = message.get("content")
        role = message.get("role")
        clear_at = message.get("clear_at")
        cleared = clear_at == "next_user_message" and user_turns and user_turns[-1] > index
        if role == "system" and cleared:
            # A turn-scoped system message renders nothing once a later user message exists
            # (one carrying only tool results counts), so a cleared one is left out entirely.
            continue
        if clear_at not in (None, "never") and (
            role != "system" or clear_at != "next_user_message"
        ):
            losses.count("system_message_expiry", f"message(s) with clear_at {clear_at} as {role}")
        output_config = _as_dict(message.get("output_config")) or {}
        if role != "assistant" and any(v is not None for v in output_config.values()):
            # Effort set partway through the conversation, from this message on; the vocabulary
            # has one request-wide reasoning setting. (An assistant message's is the API's own
            # report of the effort it ran at, echoed back.)
            losses.count("mid_conversation_effort", "message(s) changing the output config")
        if role == "assistant":
            out.append(_convert_assistant(content, losses))
        elif role == "system":
            out.append(SystemMessage(content=_system_parts(content, losses)))
        else:
            out.extend(_convert_user(content, losses))
    return out


def _system_parts(system: Any, losses: LossLog) -> tuple[ContentText, ...]:
    if isinstance(system, str):
        return (ContentText(text=system),) if system else ()
    return _leaf_blocks(system, losses)


def _echoed_thinking(block: dict[str, Any]) -> ContentReasoning:
    """A `thinking` block a harness echoed back: its signature is the provider's own, or an
    envelope carrying another form or state (see `_reasoning_envelope`)."""
    text = str(block.get("thinking", ""))
    signature = block.get("signature")
    if not isinstance(signature, str) or not signature:
        return ContentReasoning(text=text, form="summary", reasoning_state=None)
    echoed = read_text_slot(signature, AnthropicSignature)
    return ContentReasoning(text=text, form=echoed.form or "summary", reasoning_state=echoed.state)


def _convert_assistant(content: Any, losses: LossLog) -> AssistantMessage:
    if isinstance(content, str):
        return AssistantMessage(content=(ContentText(text=content),))
    blocks: list[Content] = []
    for block in _dict_list(content):
        block_type = block.get("type")
        if block_type == "tool_use":
            # A container whose children are data (`input`), not content.
            _note_toolset(block, losses)
            fields: dict[str, Any] = {
                "id": str(block.get("id", "")),
                "name": str(block.get("name", "")),
                "input": _as_dict(block.get("input")) or {},
                "parse_error": None,
            }
            cache_control = _cache_control(block.get("cache_control"))
            if cache_control is not None:
                blocks.append(CacheableContentToolUse(**fields, cache_control=cache_control))
            else:
                blocks.append(ContentToolUse(**fields))
        elif block_type == "thinking":
            blocks.append(_echoed_thinking(block))
        elif block_type == "redacted_thinking":
            blocks.append(
                ContentReasoning(
                    text="",
                    form="redacted",
                    reasoning_state=DynamicData.from_tagged(
                        AnthropicRedactedThinking(str(block.get("data", "")))
                    ),
                )
            )
        else:
            leaf = _leaf_block(block, losses)
            if leaf is not None:
                blocks.append(leaf)
    return AssistantMessage(content=tuple(blocks))


def _convert_user(content: Any, losses: LossLog) -> list[ChatMessage]:
    if isinstance(content, str):
        return [UserMessage(content=(ContentText(text=content),))]
    # Chat-completions-style providers want tool results right after the assistant's tool calls,
    # so emit them first, then any remaining user text as a single user message.
    tool_messages: list[ChatMessage] = []
    text_parts: list[ContentText] = []
    for block in _dict_list(content):
        if block.get("type") == "tool_result":
            # A container: its `content` blocks go through the same leaf handling as a
            # message's, so media in a tool result is recorded like media in a message.
            _note_toolset(block, losses)
            is_error = block.get("is_error")
            fields: dict[str, Any] = {
                "content": _tool_result_content(block.get("content"), losses),
                "tool_call_id": str(block.get("tool_use_id", "")),
                "is_error": is_error if isinstance(is_error, bool) else None,
            }
            cache_control = _cache_control(block.get("cache_control"))
            if cache_control is not None:
                tool_messages.append(CacheableToolMessage(**fields, cache_control=cache_control))
            else:
                tool_messages.append(ToolMessage(**fields))
            continue
        leaf = _leaf_block(block, losses)
        if leaf is not None:
            text_parts.append(leaf)
    out: list[ChatMessage] = list(tool_messages)
    if text_parts:
        out.append(UserMessage(content=tuple(text_parts)))
    return out


def _note_toolset(block: dict[str, Any], losses: LossLog) -> None:
    """Record the toolset tag of a call or result from an `mcp_toolset` member: the toolset is
    a provider-side tool the vocabulary leaves out, and so is the tag naming it."""
    if isinstance(block.get("toolset_name"), str):
        losses.count("toolset", "tool call/result block(s) tagged with their toolset")


def _tool_result_content(content: Any, losses: LossLog) -> tuple[ContentText, ...]:
    if isinstance(content, str):
        return (ContentText(text=content),)
    return _leaf_blocks(content, losses)


def _leaf_blocks(blocks: Any, losses: LossLog) -> tuple[ContentText, ...]:
    """The text blocks of a block list, every other leaf recorded as dropped."""
    out: list[ContentText] = []
    for block in _dict_list(blocks):
        leaf = _leaf_block(block, losses)
        if leaf is not None:
            out.append(leaf)
    return tuple(out)


def _leaf_block(block: dict[str, Any], losses: LossLog) -> ContentText | None:
    """One content block that is not a tool call or a reasoning block: its text, or None for a
    block the vocabulary has no slot for, which is recorded on `losses` as it is dropped. Every
    container in the dialect (a message, the system prompt, a tool result) sends its blocks
    here, so a child is either converted or reported. A text block's `citations` are a child
    the vocabulary cannot carry either."""
    if block.get("type") == "text":
        if block.get("citations"):
            losses.count("citations", "text block citation list(s)")
        return _text_part(block)
    _count_dropped_block(block.get("type"), losses)
    return None


def _count_dropped_block(block_type: object, losses: LossLog) -> None:
    """Record a content block the vocabulary has no slot for, at the point it is dropped."""
    if block_type == "image":
        losses.count("image", "image block(s)")
    elif block_type == "document":
        losses.count("document", "document block(s)")
    elif block_type in _PROVIDER_TOOL_BLOCK_TYPES:
        losses.count(
            "provider_tool_exchange", "provider-side tool call/result block(s) in the history"
        )
    else:
        losses.count(f"block:{block_type}", f"'{block_type}' block(s)")


def _text_part(block: dict[str, Any]) -> ContentText:
    text = str(block.get("text", ""))
    cache_control = _cache_control(block.get("cache_control"))
    if cache_control is not None:
        return CacheableContentText(text=text, cache_control=cache_control)
    return ContentText(text=text)


def _convert_tools(tools: Any, losses: LossLog) -> list[ToolParam]:
    out: list[ToolParam] = []
    deferred = 0
    for tool in _dict_list(tools):
        name = tool.get("name")
        kind = tool.get("type")
        if isinstance(kind, str) and kind != "custom":
            # Only a tool the harness runs with its own schema (`type` absent or `custom`) has a
            # vocabulary form. Provider-side tools (`web_search_*`, which also carry a `name`)
            # and the API-defined built-ins are recorded as left out.
            family = _provider_tool_family(tool)
            if family is not None:
                losses.note(f"provider_tool:{family}", f"provider-side tool {kind}")
            else:
                losses.note(
                    "builtin_tool", f"provider-defined tool declared without a schema: {kind}"
                )
            continue
        if not isinstance(name, str):
            continue
        if tool.get("defer_loading") is True:
            deferred += 1
        if not _note_callers(name, tool.get("allowed_callers"), losses):
            continue
        description = tool.get("description")
        strict = tool.get("strict")
        fields: dict[str, Any] = {
            "name": name,
            "description": description if isinstance(description, str) else None,
            "input_schema": cast("JSONObject", tool.get("input_schema") or {}),
            "strict": strict if isinstance(strict, bool) else False,
            "input_examples": _input_examples(tool.get("input_examples")),
        }
        cache_control = _cache_control(tool.get("cache_control"))
        if cache_control is not None:
            out.append(CacheableToolParam(**fields, cache_control=cache_control))
        else:
            out.append(ToolParam(**fields))
    if deferred:
        # Deferred definitions are offered whole to a translated subject (it has no tool search).
        losses.note("tool_search", f"{deferred} tool definition(s) marked defer_loading")
    return out


def _note_callers(name: str, callers: Any, losses: LossLog) -> bool:
    """Whether the model may call the tool itself; a tool also callable from code execution
    (programmatic tool calling) is recorded, since the vocabulary has only the model's own calls.
    A tool callable only from code is left out rather than offered to the model, which may not
    call it. `["direct"]` is the default and nothing is lost."""
    if not isinstance(callers, list) or callers == ["direct"]:
        return True
    listed = ", ".join(str(caller) for caller in cast("list[Any]", callers))
    if "direct" not in callers:
        losses.note("programmatic_tool_calling", f"tool {name} callable only by {listed}: left out")
        return False
    losses.note("programmatic_tool_calling", f"tool {name} callable by {listed}")
    return True


def _input_examples(value: Any) -> tuple[JSONObject, ...] | None:
    examples = _dict_list(value)
    if not isinstance(value, list) or not examples:
        return None
    return tuple(cast("JSONObject", example) for example in examples)


def _convert_tool_choice(choice: Any) -> tuple[ToolChoice | None, bool | None]:
    """The request's tool choice and `parallel_tool_calls` (Anthropic's inverted flag)."""
    data = _as_dict(choice)
    if data is None:
        return None, None
    disable = data.get("disable_parallel_tool_use")
    parallel = (not disable) if isinstance(disable, bool) else None
    kind = data.get("type")
    if kind == "auto":
        return ToolChoiceAuto(), parallel
    if kind == "any":
        return ToolChoiceRequired(), parallel
    if kind == "none":
        return ToolChoiceNone(), None
    if kind == "tool":
        name = data.get("name")
        return ToolChoiceTool(name=name if isinstance(name, str) else ""), parallel
    return None, None


def _cache_control(value: Any) -> CacheControl | None:
    data = _as_dict(value)
    if data is None:
        return None
    ttl = data.get("ttl")
    return CacheControl(ttl=ttl if isinstance(ttl, str) else None)


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


# --- response: subject-model stream -> Anthropic -------------------------------------------------

# FailureCategory -> the Anthropic (HTTP status, error type) pair. `overloaded` keeps 529
# `overloaded_error` — that is what triggers harness overload backoff.
_FAILURE_STATUS_TYPES: dict[FailureCategory, tuple[int, str]] = {
    "context_length": (400, "invalid_request_error"),
    "content_filter": (400, "invalid_request_error"),
    "rate_limited": (429, "rate_limit_error"),
    "overloaded": (529, "overloaded_error"),
    "authentication": (401, "authentication_error"),
    "invalid_request": (400, "invalid_request_error"),
    "server_error": (500, "api_error"),
    "adaptation_refused": (400, "invalid_request_error"),
}


# Anthropic stop reasons with no vocabulary variant of their own, decoded as a `StopOther`
# carrying the name: one passes through verbatim.
_ANTHROPIC_OTHER_REASONS = frozenset({"pause_turn", "model_context_window_exceeded"})


@dataclass(frozen=True)
class _RenderedStop:
    stop_reason: str
    stop_sequence: str | None = None
    rendering: StopRendering = StopRendering()


def _render_stop(stop: StopReason) -> _RenderedStop:
    """An assembled stop reason as Anthropic's (`stop_reason`, `stop_sequence`), with what that
    says other than as given. One mapping serves the encoders and `render_stop`."""
    if isinstance(stop, StopMaxTokens):
        return _RenderedStop("max_tokens")
    if isinstance(stop, StopStopSequence):
        return _RenderedStop("stop_sequence", stop.stop_sequence)
    if isinstance(stop, StopToolUse):
        return _RenderedStop("tool_use")
    if isinstance(stop, StopRefusal):
        # Anthropic's refusal stop does not say whether the response is the whole refusal or
        # was cut off.
        if stop.complete is None:
            return _RenderedStop("refusal")
        said = "was the whole answer" if stop.complete else "cut the response off"
        loss = ResponseLoss(
            "refusal_completeness",
            f"a refusal ({stop.provider_reason}) the provider said {said}, sent as the refusal "
            "stop reason, which does not say",
        )
        return _RenderedStop("refusal", rendering=StopRendering(losses=(loss,)))
    if isinstance(stop, StopContentFilter):
        # One stop reason covers a model's refusal and its classifiers' blocks; a content filter
        # that is not a refusal has no closer form.
        loss = ResponseLoss(
            "content_filter_as_refusal",
            f"a content filter ({stop.provider_reason}) sent as the refusal stop reason",
        )
        return _RenderedStop("refusal", rendering=StopRendering(losses=(loss,)))
    if isinstance(stop, StopOther):
        if stop.provider_reason in _ANTHROPIC_OTHER_REASONS:
            return _RenderedStop(stop.provider_reason)
        # A stop the dialect has no reason for (Gemini's MALFORMED_FUNCTION_CALL, say): end_turn
        # would read as a finished answer, so the call is answered as an error.
        loss = ResponseLoss(
            "stop_reason",
            f"a stop with no Anthropic form ({stop.provider_reason}) answered as an error",
        )
        return _RenderedStop("end_turn", rendering=StopRendering(losses=(loss,), as_error=True))
    return _RenderedStop("end_turn")


def _stop_details(stop: StopReason) -> dict[str, Any] | None:
    """Anthropic's `stop_details`: what a refusal said about itself, when the provider gave it.
    Another provider's filter detail (Gemini's safety ratings) is that provider's own JSON, with
    no Anthropic form."""
    if (
        isinstance(stop, StopRefusal)
        and stop.provider_reason == "refusal"
        and stop.detail is not None
    ):
        return dict(stop.detail)
    return None


def _usage_fields(usage: Usage) -> dict[str, Any]:
    """The Anthropic `usage` object; `Usage.input_tokens` is already non-cached (same semantics).
    Reasoning tokens ride in `output_tokens_details.thinking_tokens`, where the API reports
    them."""
    out: dict[str, Any] = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
    if usage.cache_read_input_tokens is not None:
        out["cache_read_input_tokens"] = usage.cache_read_input_tokens
    if usage.cache_write_input_tokens is not None:
        out["cache_creation_input_tokens"] = usage.cache_write_input_tokens
    if usage.reasoning_output_tokens is not None:
        out["output_tokens_details"] = {"thinking_tokens": usage.reasoning_output_tokens}
    if usage.speed is not None:
        out["speed"] = usage.speed
    return out


def sse_event(event_type: str, data: object) -> bytes:
    """Encode one Anthropic SSE event (`event:`/`data:` lines, blank-line terminated)."""
    payload = json.dumps(data, separators=(",", ":"))
    return f"event: {event_type}\ndata: {payload}\n\n".encode()


def _new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


_OpenKind = Literal["text", "tool_use", "thinking"]


def _harness_signature(form: ReasoningForm, state: DynamicData | None) -> str:
    """The signature of a `thinking` block sent to a harness: the provider's own signature when
    the state is one, so the harness can resume directly against Anthropic, and otherwise an
    envelope with the block's form and state, which the harness echoes back like any
    signature."""
    signature = own_reasoning_state(state, AnthropicSignature)
    return signature.signature if signature is not None else envelope_text(form, state)


def _harness_reasoning_block(
    text: str, form: ReasoningForm, state: DynamicData | None
) -> dict[str, object]:
    """A reasoning block as a harness receives it: `redacted_thinking` for Anthropic's own
    redacted reasoning, a `thinking` block for everything else."""
    redacted = own_reasoning_state(state, AnthropicRedactedThinking)
    if redacted is not None:
        return {"type": "redacted_thinking", "data": redacted.data}
    return {"type": "thinking", "thinking": text, "signature": _harness_signature(form, state)}


class _StreamState:
    """Tracks which Anthropic content block is open while replaying subject-model deltas.

    Anthropic streams exactly one content block at a time; the subject-model stream interleaves a
    text delta stream with tool-call deltas keyed by an integer tool ordinal. This maps tool
    ordinals to Anthropic block indices and closes the currently-open block before opening the
    next.

    A reasoning block opens as a `thinking` block on `StreamReasoningStart`, and its text streams
    as it arrives; its state arrives whole and is written as the block's signature when the block
    closes (see `_harness_signature`). A reasoning delta with no start before it opens a block of
    literal reasoning. Redacted reasoning has no text to stream, so its block is written whole once
    its state arrives.
    """

    def __init__(self) -> None:
        self._open_index: int | None = None
        self._open_kind: _OpenKind | None = None
        self._next_index = 0
        self._tool_block: dict[int, int] = {}
        self._reasoning_form: ReasoningForm = "full"
        self._reasoning_state: DynamicData | None = None
        self._pending_redacted = False
        self.saw_tool_call = False

    def process(self, event: StreamEvent) -> list[bytes]:
        if isinstance(event, StreamReasoningStart):
            out = self._flush_redacted() + self._close_open()
            if event.form == "redacted":
                self._pending_redacted = True
                return out
            return out + self._open_thinking(event.form)
        if isinstance(event, StreamReasoningState):
            if self._pending_redacted:
                self._pending_redacted = False
                return self._whole_block(_harness_reasoning_block("", "redacted", event.state))
            out: list[bytes] = []
            if self._open_kind != "thinking" or self._reasoning_state is not None:
                out += self._close_open() + self._open_thinking("summary")
            self._reasoning_state = event.state
            return out
        out = self._flush_redacted()
        if isinstance(event, StreamReasoningDelta) and event.text:
            if self._open_kind != "thinking":
                out += self._close_open() + self._open_thinking("full")
            out.append(
                sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._open_index,
                        "delta": {"type": "thinking_delta", "thinking": event.text},
                    },
                )
            )
        elif isinstance(event, StreamTextDelta) and event.text:
            if self._open_kind != "text":
                out += self._close_open()
                index = self._alloc()
                self._open_index, self._open_kind = index, "text"
                out.append(
                    sse_event(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                )
            out.append(
                sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self._open_index,
                        "delta": {"type": "text_delta", "text": event.text},
                    },
                )
            )
        elif isinstance(event, StreamToolUseStart):
            self.saw_tool_call = True
            out += self._close_open()
            index = self._alloc()
            self._tool_block[event.index] = index
            self._open_index, self._open_kind = index, "tool_use"
            out.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": event.id,
                            "name": event.name,
                            "input": {},
                        },
                    },
                )
            )
        elif isinstance(event, StreamToolUseInputDelta) and event.partial_json:
            block_index = self._tool_block.get(event.index)
            if block_index is not None:
                out.append(
                    sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": block_index,
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": event.partial_json,
                            },
                        },
                    )
                )
        return out

    def _alloc(self) -> int:
        index = self._next_index
        self._next_index += 1
        return index

    def _open_thinking(self, form: ReasoningForm) -> list[bytes]:
        index = self._alloc()
        self._open_index, self._open_kind = index, "thinking"
        self._reasoning_form, self._reasoning_state = form, None
        return [
            sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
            )
        ]

    def _whole_block(self, block: dict[str, object]) -> list[bytes]:
        """A block written in one piece: started with all its content, then stopped."""
        out = self._close_open()
        index = self._alloc()
        if block["type"] == "thinking":
            start: dict[str, object] = {"type": "thinking", "thinking": "", "signature": ""}
            out.append(
                sse_event(
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": start},
                )
            )
            out.append(
                sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "signature_delta", "signature": block["signature"]},
                    },
                )
            )
        else:
            out.append(
                sse_event(
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": block},
                )
            )
        out.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": index}))
        return out

    def _flush_redacted(self) -> list[bytes]:
        """A redacted block whose state never arrived, written with no state."""
        if not self._pending_redacted:
            return []
        self._pending_redacted = False
        return self._whole_block(_harness_reasoning_block("", "redacted", None))

    def _close_open(self) -> list[bytes]:
        if self._open_index is None:
            return []
        index, kind = self._open_index, self._open_kind
        self._open_index, self._open_kind = None, None
        out: list[bytes] = []
        if kind == "thinking":
            signature = _harness_signature(self._reasoning_form, self._reasoning_state)
            self._reasoning_state = None
            out.append(
                sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "signature_delta", "signature": signature},
                    },
                )
            )
        out.append(sse_event("content_block_stop", {"type": "content_block_stop", "index": index}))
        return out

    def close(self) -> list[bytes]:
        return self._flush_redacted() + self._close_open()


async def synthesize_anthropic_sse(
    model: str, items: AsyncIterator[SubjectStreamItem]
) -> AsyncIterator[bytes]:
    """Replay a subject-model stream as the Anthropic streaming SSE event sequence.

    A terminal `ModelCallFailure` (reached after deltas already streamed) is surfaced as an
    Anthropic `error` SSE event — matching how the real API reports a mid-stream overload — rather
    than an HTTP status (the status is already committed). Failures with no prior deltas are
    handled by the backend before this is called.
    """
    yield sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": _new_message_id(),
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    state = _StreamState()
    result: ModelResponse | ModelCallFailure | None = None
    async for item in items:
        if isinstance(item, (ModelResponse, ModelCallFailure)):
            result = item
            break
        for frame in state.process(item):
            yield frame
    for frame in state.close():
        yield frame
    if not isinstance(result, ModelResponse):
        failure = result or ModelCallFailure(
            category="server_error",
            description="subject-model stream ended without a result",
            provider_http_status=None,
        )
        _, error_type = _FAILURE_STATUS_TYPES[failure.category]
        yield sse_event(
            "error",
            {"type": "error", "error": {"type": error_type, "message": failure_message(failure)}},
        )
        return
    stop = _render_stop(result.stop_reason)
    stop_reason, stop_sequence = stop.stop_reason, stop.stop_sequence
    if state.saw_tool_call and stop_reason == "end_turn":
        stop_reason = "tool_use"
    delta: dict[str, Any] = {"stop_reason": stop_reason, "stop_sequence": stop_sequence}
    stop_details = _stop_details(result.stop_reason)
    if stop_details is not None:
        delta["stop_details"] = stop_details
    yield sse_event(
        "message_delta",
        {"type": "message_delta", "delta": delta, "usage": _usage_fields(result.usage)},
    )
    yield sse_event("message_stop", {"type": "message_stop"})


def response_to_anthropic_message(model: str, response: ModelResponse) -> dict[str, object]:
    """Render an assembled response as a non-streaming Anthropic `Message` body.

    Unlike the streamed path, the full usage is known here, so `input_tokens` is faithful. The
    response id is not echoed (it may be another provider's shape); a fresh `msg_` id is
    synthesized, and the real id stays on the `ModelResponse`.
    """
    content: list[dict[str, object]] = []
    saw_tool_call = False
    for message in response.output:
        if not isinstance(message, AssistantMessage):
            continue  # server-side tool exchanges have no representation here
        for block in message.content:
            if isinstance(block, ContentText):
                if block.text:
                    content.append({"type": "text", "text": block.text})
            elif isinstance(block, ContentReasoning):
                content.append(
                    _harness_reasoning_block(block.text, block.form, block.reasoning_state)
                )
            elif isinstance(block, ContentToolUse):
                saw_tool_call = True
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": dict(block.input),
                    }
                )
    stop = _render_stop(response.stop_reason)
    stop_reason, stop_sequence = stop.stop_reason, stop.stop_sequence
    if saw_tool_call and stop_reason == "end_turn":
        stop_reason = "tool_use"
    rendered: dict[str, object] = {
        "id": _new_message_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "usage": cast("dict[str, object]", _usage_fields(response.usage)),
    }
    stop_details = _stop_details(response.stop_reason)
    if stop_details is not None:
        rendered["stop_details"] = stop_details
    return rendered


def anthropic_failure_body(failure: ModelCallFailure) -> tuple[int, bytes]:
    """A request-time failure as the dialect's (HTTP status, error body)."""
    status, error_type = _FAILURE_STATUS_TYPES[failure.category]
    return status, anthropic_error_body(error_type, failure_message(failure))


def anthropic_error_body(anthropic_type: str, message: str) -> bytes:
    """Encode an Anthropic error body (`{"type":"error","error":{...}}`)."""
    return json.dumps(
        {"type": "error", "error": {"type": anthropic_type, "message": message}}
    ).encode("utf-8")


_ENDPOINT = "/v1/messages"


class AnthropicMessagesCodec:
    """The Anthropic Messages dialect (`/v1/messages`), both halves.

    Args:
        default_max_tokens: The `max_tokens` rendered when a request specifies none (the API
            requires a value); a declared default, applied only where the request left it absent.
            The other declared default, `thinking.display: "summarized"` when thinking is on, is
            fixed (see `encode_anthropic_request`).
    """

    dialect: Dialect = "anthropic_messages"
    reasoning_round_trip = True

    def __init__(self, *, default_max_tokens: int = 16384) -> None:
        self._default_max_tokens = default_max_tokens

    # --- model side -----------------------------------------------------------------------------

    def adapt_request(self, model: str, request: ModelRequest) -> AdaptedRequest:
        return adapt_anthropic_request(
            model, request, lambda state: self.accepts_reasoning_state(state, model)
        )

    def encode_request(
        self, model: str, request: ModelRequest, *, streaming: bool
    ) -> EncodedRequest:
        return encode_anthropic_request(
            model, request, streaming=streaming, default_max_tokens=self._default_max_tokens
        )

    def sse_decoder(self) -> SseDecoder:
        return AnthropicSseDecoder()

    def decode_response(self, body: bytes) -> ModelResponse | ModelCallFailure:
        return decode_anthropic_message(body)

    def decode_failure(self, status: int, body: bytes) -> ModelCallFailure:
        return decode_anthropic_failure(status, body)

    def accepts_reasoning_state(self, state: DynamicData, model: str) -> bool:
        return (
            own_reasoning_state(state, AnthropicSignature) is not None
            or own_reasoning_state(state, AnthropicRedactedThinking) is not None
        )

    # --- harness side ---------------------------------------------------------------------------

    def matches_path(self, target: str) -> bool:
        return path_matches(target, _ENDPOINT)

    def matches_headers(self, headers: Headers) -> bool:
        return has_header(headers, "anthropic-version")

    def matches_body(self, body: JSONObject) -> bool:
        # Both `messages` dialects share the key; a top-level `system` or a required `max_tokens`
        # is Anthropic-only.
        return "messages" in body and ("system" in body or "max_tokens" in body)

    def route(self, target: str, body: JSONObject) -> Route:
        path = normalized_path(target)
        model = body.get("model")
        streaming = bool(body.get("stream", False))
        if path.endswith(_ENDPOINT + "/count_tokens"):
            kind: Literal["completion", "count_tokens", "other"] = "count_tokens"
        elif path == _ENDPOINT or path.endswith(_ENDPOINT):
            kind = "completion"
        else:
            kind = "other"
        return Route(
            model=model if isinstance(model, str) else None, streaming=streaming, kind=kind
        )

    def decode_request(self, target: str, headers: Headers, body: bytes) -> DecodedRequest:
        return anthropic_request_to_canonical(_json_object(body), headers, target)

    def provider_tools(self, body: JSONObject) -> tuple[ProviderTool, ...]:
        return provider_tools_in_request(cast("dict[str, Any]", body))

    def strip_reasoning(self, request: ModelRequest) -> ModelRequest:
        # `output_config` also carries structured-output settings (`format`); only `effort` is
        # reasoning configuration.
        return without_reasoning(request, (("thinking",), ("output_config", "effort")))

    def encode_sse(
        self,
        model: str,
        items: AsyncIterator[SubjectStreamItem],
        *,
        tools: tuple[ToolParam, ...] = (),
    ) -> AsyncIterator[bytes]:
        return synthesize_anthropic_sse(model, items)  # calls render by name alone in this dialect

    def keepalive_frame(self) -> bytes | None:
        # The API's own keepalive, which the SDKs and CLIs read and skip.
        return sse_event("ping", {"type": "ping"})

    def encode_response(
        self, model: str, response: ModelResponse, *, tools: tuple[ToolParam, ...] = ()
    ) -> JSONObject:
        return cast("JSONObject", response_to_anthropic_message(model, response))

    def encode_failure(self, failure: ModelCallFailure) -> tuple[int, bytes]:
        return anthropic_failure_body(failure)

    def render_stop(self, stop: StopReason, *, streaming: bool) -> StopRendering:
        return _render_stop(stop).rendering

    def encode_error(self, status: int, error_type: str, message: str) -> bytes:
        return anthropic_error_body(error_type, message)


def _json_object(body: bytes) -> dict[str, Any]:
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("request body is not a JSON object")
    return cast("dict[str, Any]", parsed)


# --- model side: ModelRequest -> Anthropic request body ------------------------------------------
#
# Rendering notes. The encoder renders a request as it is: everything the Messages wire format can
# express, whether or not the API would accept it (another provider's signed reasoning as a
# `thinking` block, a fifth breakpoint), so a decoded harness request re-encodes as sent. What the
# API does not accept is changed beforehand by `adapt_anthropic_request`, which records each change.
# What the wire format cannot express at all is a `CodecError` (`_common.unadapted`).
#
# - Cache breakpoints are taken from `CacheableContentText` / `CacheableToolParam` /
#   `CacheableToolMessage` (see `behaviors.types.content`, `behaviors.types.tool`, and
#   `behaviors.types.message`). Adaptation removes one on an empty text block, which the API
#   rejects, and any beyond the latest four.
#   A breakpoint without a TTL is the API's default, 5 minutes. A TTL is the provider's
#   retention setting, not part of the prompt, and a minimum. Adaptation gives a tool result and
#   its marked final block, which end at the same boundary, the longer of their TTLs; fits a TTL
#   the API does not offer (an OpenAI `30m` or `12h`) to the shortest it offers that is at least
#   as long, else the longest; then shortens a TTL longer than one before it, which the API
#   rejects. Each change is a delivery loss.
# - Leading `SystemMessage`s render as the top-level `system` parameter; one that follows the
#   first user or assistant message renders as a `system`-role message at its position. Not
#   every model accepts a mid-conversation system message (the API answers with its own
#   `invalid_request` naming the model), and rendering it where it was is what the request
#   asked for; hoisting it would silently move an instruction to the start of the conversation.
# - Consecutive user-role messages (`UserMessage` / `ToolMessage`) merge into one API message,
#   the inverse of how tool results fan out into individual `ToolMessage`s on decoding.
# - Assistant reasoning is sent back only with its Anthropic state: an `AnthropicSignature` as a
#   `thinking` block, an `AnthropicRedactedThinking` as a `redacted_thinking` block. Adaptation
#   removes reasoning with any other provider's state (`accepts_reasoning_state`) or with none,
#   which the API has no representation for.
# - `SamplingParams.speed` renders as `speed` (fast mode), with the `fast-mode-2026-02-01` beta
#   header added unless the request's extras already carry it.
# - `SamplingParams.reasoning` maps to the `thinking` configuration (disabled /
#   enabled-with-budget / adaptive) and, when an effort level is given, to `output_config.effort`
#   (adaptation sends `minimal` as `low`; the rest of the neutral vocabulary is Anthropic's own).
#   Adaptive
#   thinking (enabled with no budget) exists only on newer models; older ones 400 on it.
# - Sampling parameters are rendered exactly as requested; a model that rejects a value answers
#   with its own error. `SamplingParams.extra` recognizes `top_k`; any other key is forwarded
#   verbatim for the provider to validate.


_FAST_MODE_BETA = "fast-mode-2026-02-01"
"""The beta the API requires for the `speed` parameter."""


def _carries_beta(headers: Headers, beta: str) -> bool:
    """Whether an `anthropic-beta` header among `headers` already lists `beta`."""
    return any(
        name.lower() == "anthropic-beta" and beta in {part.strip() for part in value.split(",")}
        for name, value in headers
    )


def _thinking_param(reasoning: ReasoningParams | None) -> dict[str, Any] | None:
    """The `thinking` request parameter for a neutral reasoning config, or None to omit it.
    `display` is settled afterwards (`_apply_summary_choice`), once a same-dialect extra has
    had its say. Off is spelled `disabled`, the canonical spelling; a model that takes another
    (`between_tools`) says so in its 400, and the provider implementation adapts."""
    if reasoning is None:
        return None
    if not reasoning.enabled:
        return {"type": "disabled"}
    if reasoning.budget_tokens is not None:
        return {"type": "enabled", "budget_tokens": reasoning.budget_tokens}
    return {"type": "adaptive"}


def _apply_summary_choice(thinking_body: dict[str, Any], reasoning: ReasoningParams | None) -> None:
    """Settle `thinking.display` on a rendered request with thinking on, reconciling the
    neutral choice with what a same-dialect extra retained: `False` renders `omitted` over
    anything retained, `True` keeps a retained `summarized` and renders it otherwise (over an
    unknown value too), unspecified honors a retained value, else the declared default
    `summarized`, without which current models return their thinking blocks signed but empty."""
    choice = reasoning.summary if reasoning is not None else None
    if choice is None:
        thinking_body.setdefault("display", "summarized")
    else:
        thinking_body["display"] = _DISPLAY_OF_SUMMARY[choice]


def _output_config_param(reasoning: ReasoningParams | None) -> dict[str, str] | None:
    """The `output_config` parameter carrying the effort level, or None to omit it."""
    if reasoning is None or not reasoning.enabled or reasoning.effort is None:
        return None
    return {"effort": reasoning.effort}


def _cache_control_param(cache_control: CacheControl) -> dict[str, Any]:
    param: dict[str, Any] = {"type": cache_control.type}
    if cache_control.ttl is not None:
        param["ttl"] = cache_control.ttl
    return param


def _text_block_param(block: ContentText) -> dict[str, Any]:
    param: dict[str, Any] = {"type": "text", "text": block.text}
    if isinstance(block, CacheableContentText) and block.cache_control is not None:
        param["cache_control"] = _cache_control_param(block.cache_control)
    return param


def _output_format_param(response_format: ResponseFormat | None) -> dict[str, Any] | None:
    """`output_config.format` for a JSON-schema response format. Plain text is the default and
    needs nothing. JSON without a schema, an advisory schema and a format's description have no
    Anthropic form (the API always enforces a schema), so adapting the request handles them."""
    if isinstance(response_format, ResponseFormatJsonSchema):
        if not response_format.strict:
            raise unadapted("an advisory (non-strict) response schema")
        if response_format.description is not None:
            raise unadapted("a response format's description")
        return {"type": "json_schema", "schema": copy.deepcopy(dict(response_format.schema))}
    if isinstance(response_format, ResponseFormatJsonObject):
        raise unadapted("a JSON response format without a schema")
    return None


def _assistant_block_params(
    message: AssistantMessage, names: ToolNames | None = None
) -> list[dict[str, Any]]:
    names = names if names is not None else ToolNames()
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, ContentText):
            blocks.append(_text_block_param(block))
        elif isinstance(block, ContentReasoning):
            signature = own_reasoning_state(block.reasoning_state, AnthropicSignature)
            redacted = own_reasoning_state(block.reasoning_state, AnthropicRedactedThinking)
            if signature is not None:
                blocks.append(
                    {"type": "thinking", "thinking": block.text, "signature": signature.signature}
                )
            elif redacted is not None:
                blocks.append({"type": "redacted_thinking", "data": redacted.data})
            else:
                raise unadapted("reasoning without Anthropic state")
        elif isinstance(block, ContentToolUse):
            param: dict[str, Any] = {
                "type": "tool_use",
                "id": block.id,
                "name": names.wire_name(block.name, block.namespace),
                "input": copy.deepcopy(dict(block.input)),
            }
            if isinstance(block, CacheableContentToolUse) and block.cache_control is not None:
                param["cache_control"] = _cache_control_param(block.cache_control)
            blocks.append(param)
    return blocks


def _tool_result_block_param(message: ToolMessage) -> dict[str, Any]:
    param: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": message.tool_call_id,
        "content": [_text_block_param(block) for block in message.content],
    }
    if message.is_error is not None:
        param["is_error"] = message.is_error
    if isinstance(message, CacheableToolMessage) and message.cache_control is not None:
        param["cache_control"] = _cache_control_param(message.cache_control)
    return param


def _render_context(
    context: tuple[ChatMessage, ...], names: ToolNames | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Render messages into the top-level `system` blocks and the messages list. `names` are
    the request's tool declarations, which give a grouped tool's call its wire name here."""
    system_blocks: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    # Blocks of the current run of user-role messages, merged into one API message when the run
    # ends.
    user_blocks: list[dict[str, Any]] = []

    def flush_user_blocks() -> None:
        if user_blocks:
            messages.append({"role": "user", "content": list(user_blocks)})
            user_blocks.clear()

    for message in context:
        if isinstance(message, SystemMessage):
            blocks = [_text_block_param(block) for block in message.content]
            if messages or user_blocks:
                flush_user_blocks()
                messages.append({"role": "system", "content": blocks})
            else:
                system_blocks.extend(blocks)
        elif isinstance(message, UserMessage):
            user_blocks.extend(_text_block_param(block) for block in message.content)
        elif isinstance(message, ToolMessage):
            user_blocks.append(_tool_result_block_param(message))
        else:
            flush_user_blocks()
            messages.append(
                {"role": "assistant", "content": _assistant_block_params(message, names)}
            )
    flush_user_blocks()
    return system_blocks, messages


def _tool_param(tool: ToolParam, names: ToolNames | None = None) -> dict[str, Any]:
    # A freeform tool arrives with the one-string-field schema the vocabulary gives it, and a
    # grouped tool under its wire name (its own, unless two groups share it) with its group's
    # description folded into its own.
    wire = names.wire_name(tool.name, tool.namespace) if names is not None else tool.name
    param: dict[str, Any] = {
        "name": wire,
        "input_schema": copy.deepcopy(dict(tool.input_schema)),
    }
    description = flattened_description(tool)
    if description is not None:
        param["description"] = description
    if tool.strict == "auto":
        raise unadapted(f"tool {tool.name}'s automatic strictness")
    if tool.strict:
        param["strict"] = True
    if tool.input_examples is not None:
        param["input_examples"] = [copy.deepcopy(dict(example)) for example in tool.input_examples]
    if isinstance(tool, CacheableToolParam) and tool.cache_control is not None:
        param["cache_control"] = _cache_control_param(tool.cache_control)
    if tool.output_schema is not None:
        raise unadapted(f"tool {tool.name}'s output schema")
    return param


def _tool_choice_param(
    tool_choice: ToolChoice | None,
    parallel_tool_calls: bool | None,
    has_tools: bool,
    names: ToolNames,
) -> dict[str, Any] | None:
    param: dict[str, Any] | None = None
    if isinstance(tool_choice, ToolChoiceAuto):
        param = {"type": "auto"}
    elif isinstance(tool_choice, ToolChoiceRequired):
        param = {"type": "any"}
    elif isinstance(tool_choice, ToolChoiceTool):
        param = {"type": "tool", "name": names.choice_wire_name(tool_choice.name)}
    elif isinstance(tool_choice, ToolChoiceNone):
        param = {"type": "none"}
    if parallel_tool_calls is not None and has_tools:
        # Anthropic expresses parallel tool use as an inverted flag on the tool_choice object
        # (not valid on "none").
        if param is None:
            param = {"type": "auto"}
        if param["type"] != "none":
            param["disable_parallel_tool_use"] = not parallel_tool_calls
    return param


# The API rejects a request with more than four `cache_control` breakpoints. A dialect that marks
# its prefix as it grows (Responses, whose API reads breakpoints from the whole conversation)
# can bring more.
_MAX_CACHE_BREAKPOINTS = 4
# The lifetimes the API offers, shortest first. A longer TTL may not follow a shorter one in render
# order; a breakpoint without a TTL is 5 minutes
# (https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
CACHE_TTLS = ("5m", "1h")
DEFAULT_CACHE_TTL = "5m"


def _rendered_blocks(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from _dict_list(body.get("tools"))
    yield from _dict_list(body.get("system"))
    for message in _dict_list(body.get("messages")):
        for block in _dict_list(message.get("content")):
            yield block
            if block.get("type") == "tool_result":
                yield from _dict_list(block.get("content"))


def adapt_anthropic_request(
    model: str, request: ModelRequest, accepts: ReasoningStateFilter
) -> AdaptedRequest:
    """`request` changed so the Messages API can carry it, one loss per change.

    The API takes back only its own reasoning: a `thinking` block it signed or a `redacted_thinking`
    block, so reasoning with another provider's state (which `accepts`, the codec's
    `accepts_reasoning_state` for `model`, rejects) or with none is left out. Effort `minimal` is
    sent as `low`, its nearest level. A turn's tool results go first, as the API requires.
    Breakpoints go where the API accepts them: not on an empty text block, at most the latest four,
    and none with a longer TTL than one before it (shortened to it), after a tool result and its
    marked final block share the longer of their TTLs and each TTL is fitted to one the API offers.
    """
    adaptation = AdaptedRequestBuilder(request)
    drop_foreign_extras(adaptation, "anthropic_messages")
    drop_foreign_reasoning(adaptation, accepts)
    drop_stateless_reasoning(adaptation, "Anthropic signature")
    reasoning = adaptation.request.sampling.reasoning
    if reasoning is not None and reasoning.enabled and reasoning.effort == "minimal":
        replace_reasoning(
            adaptation,
            replace(reasoning, effort="low"),
            "reasoning_effort",
            "effort minimal sent as low, the lowest level the API offers",
        )
    note_flattened_tools(adaptation)
    tool_results_first(adaptation)
    drop_breakpoints(
        adaptation,
        lambda value, _: isinstance(value, ContentText) and not value.text,
        "on empty text blocks",
    )
    marked = sum(1 for _ in _breakpoints(adaptation.request))
    excess = marked - _MAX_CACHE_BREAKPOINTS
    if excess > 0:
        seen = 0

        def earliest(_value: object, _cache_control: object) -> bool:
            nonlocal seen
            seen += 1
            return seen <= excess

        drop_breakpoints(adaptation, earliest, f"beyond the latest {_MAX_CACHE_BREAKPOINTS}")
    merge_tool_result_breakpoints(adaptation, DEFAULT_CACHE_TTL)
    fit_cache_ttls(adaptation, CACHE_TTLS)
    order_cache_ttls(adaptation, DEFAULT_CACHE_TTL)
    drop_json_mode(adaptation)
    # Fitted first: a schema Anthropic cannot take is left out, not first recorded as enforced.
    fit_response_schema_to_anthropic(adaptation)
    enforce_response_format(adaptation)
    resolve_automatic_strictness(adaptation)
    unenforce_incompatible_strict_tools(adaptation, strict_problems("anthropic"))
    drop_output_schemas(adaptation, "tool_output_schema", "no slot for it")
    return adaptation.result()


def _breakpoints(request: ModelRequest) -> Iterator[object]:
    """The request's marked tools, messages and blocks, in the order `drop_breakpoints` visits
    them (the order a prompt renders in)."""
    for tool in request.tools:
        if breakpoint_of(tool) is not None:
            yield tool
    for message in request.context:
        for block in message.content:
            if breakpoint_of(block) is not None:
                yield block
        if breakpoint_of(message) is not None:
            yield message


def encode_anthropic_request(
    model: str, request: ModelRequest, *, streaming: bool, default_max_tokens: int
) -> EncodedRequest:
    """Render a request as a `/v1/messages` request for `model`.

    Everything the request asked for is rendered as asked, then extras of this dialect are
    merged in (see `apply_dialect_extras`). Two declared defaults fill in only where the request
    left them absent: `max_tokens`, which the API requires (`default_max_tokens`), and
    `thinking.display: "summarized"` when thinking is on and `ReasoningParams.summary` chose
    nothing (an explicit choice overrides a retained display, `_apply_summary_choice`), without
    which current models return their thinking blocks signed but empty.
    """
    sampling = request.sampling
    names = ToolNames(request.tools, flattened=True)
    system, messages = _render_context(request.context, names)
    body: dict[str, Any] = {"model": model, "messages": messages}
    if system:
        body["system"] = system
    if sampling.max_tokens is not None:
        body["max_tokens"] = sampling.max_tokens
    if request.tools:
        body["tools"] = [_tool_param(tool, names) for tool in request.tools]
    tool_choice = _tool_choice_param(
        request.tool_choice, request.parallel_tool_calls, bool(request.tools), names
    )
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if sampling.temperature is not None:
        body["temperature"] = sampling.temperature
    if sampling.top_p is not None:
        body["top_p"] = sampling.top_p
    if sampling.stop_sequences:
        body["stop_sequences"] = list(sampling.stop_sequences)
    if sampling.speed is not None:
        body["speed"] = sampling.speed
    thinking = _thinking_param(sampling.reasoning)
    if thinking is not None:
        body["thinking"] = thinking
    output_config: dict[str, Any] = dict(_output_config_param(sampling.reasoning) or {})
    output_format = _output_format_param(request.response_format)
    if output_format is not None:
        output_config["format"] = output_format
    if output_config:
        body["output_config"] = output_config
    extra = dict(sampling.extra)
    top_k = extra.pop("top_k", None)
    if top_k is not None:
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            raise ModelError(f"SamplingParams.extra['top_k'] must be an int, got {top_k!r}")
        body["top_k"] = top_k
    if streaming:
        body["stream"] = True
    merge_provider_params(body, extra)
    headers, query = apply_dialect_extras(body, request.extras, "anthropic_messages")
    if sampling.speed is not None and not _carries_beta(headers, _FAST_MODE_BETA):
        headers = (*headers, ("anthropic-beta", _FAST_MODE_BETA))
    apply_declared_defaults(body, {"max_tokens": default_max_tokens})
    thinking_body = _as_dict(body.get("thinking"))
    if thinking_body is not None:
        if thinking_body.get("type") in ("enabled", "adaptive"):
            _apply_summary_choice(thinking_body, sampling.reasoning)
        elif sampling.reasoning is not None and sampling.reasoning.summary is not None:
            # Thinking disabled takes precedence over an explicit summary choice: a retained
            # display goes too (an unspecified choice keeps one the harness sent).
            thinking_body.pop("display", None)
    return EncodedRequest(
        path=_ENDPOINT, body=cast("JSONObject", body), query=query, headers=headers
    )


# --- model side: Anthropic SSE / message body -> ModelResponse ------------------------------------

_MAX_FAILURE_DESCRIPTION = 500


@dataclass
class _BlockState:
    """One content block of a streamed response: the block as the provider described it, with
    text/thinking/signature/citation deltas applied in place, plus the input-JSON fragments (the
    block's `input` is set from them only once assembled, whatever the block's type)."""

    raw: dict[str, Any]
    json_parts: list[str] = field(default_factory=list[str])


def _signature_state(raw: dict[str, Any]) -> DynamicData | None:
    """A provider `thinking` block's signature as reasoning state; None for an unsigned block."""
    signature = _as_str(raw.get("signature"))
    return DynamicData.from_tagged(AnthropicSignature(signature)) if signature else None


def _redacted_state(raw: dict[str, Any]) -> DynamicData:
    return DynamicData.from_tagged(AnthropicRedactedThinking(str(raw.get("data", ""))))


# Progress-style deltas (thinking token counts under a beta) arrive many times per response, so
# each unsupported delta type is reported once per process rather than once per event.
_warned_delta_types: set[str] = set()


class _MessageAssembler:
    """Turns Anthropic stream events (parsed JSON) into StreamEvents and a ModelResponse, and
    keeps the raw view of the message the frames described.

    The raw view is built from the provider's own objects, not from the modeled fields: every
    key of `message_start.message`, of each `content_block_start` block and of `usage` survives
    (citations, nested cache-creation breakdowns, fields this codec does not model), with only
    the streamed deltas applied. That is what makes it usable as the reference side of a
    reconstruction check.
    """

    def __init__(self) -> None:
        self._message: dict[str, Any] | None = None  # minus content and usage, kept below
        self._usage: dict[str, Any] = {}
        self._blocks: dict[int, _BlockState] = {}
        self._block_order: list[int] = []
        self._tool_ordinals: dict[int, int] = {}

    @classmethod
    def from_message(cls, data: dict[str, Any]) -> _MessageAssembler:
        """An assembler pre-loaded with a complete (non-streamed) `Message` body."""
        self = cls()
        self._load_message(data)
        for index, block in enumerate(_dict_list(data.get("content"))):
            self._start_block(index, block)
        return self

    def _load_message(self, message: dict[str, Any]) -> None:
        self._message = {
            key: copy.deepcopy(value)
            for key, value in message.items()
            if key not in ("content", "usage")
        }
        usage = _as_dict(message.get("usage"))
        if usage is not None:
            self._merge_usage(usage)

    def _merge_usage(self, usage: dict[str, Any]) -> None:
        # Anthropic streams usage as successive snapshots of the fields it re-sends, not as
        # increments; later snapshots overwrite field by field.
        for key, value in usage.items():
            self._usage[key] = copy.deepcopy(value)

    def _start_block(self, index: int, block: dict[str, Any]) -> list[StreamEvent]:
        state = _BlockState(raw=copy.deepcopy(block))
        self._blocks[index] = state
        self._block_order.append(index)
        raw = state.raw
        kind = raw.get("type")
        out: list[StreamEvent] = []
        if kind == "text":
            text = str(raw.get("text", ""))
            if text:
                out.append(StreamTextDelta(text=text))
        elif kind == "thinking":
            out.append(StreamReasoningStart(form="summary"))
            thinking = str(raw.get("thinking", ""))
            if thinking:
                out.append(StreamReasoningDelta(text=thinking))
        elif kind == "redacted_thinking":
            out.append(StreamReasoningStart(form="redacted"))
            out.append(StreamReasoningState(state=_redacted_state(raw)))
        elif kind == "tool_use":
            ordinal = len(self._tool_ordinals)
            self._tool_ordinals[index] = ordinal
            out.append(
                StreamToolUseStart(
                    index=ordinal, id=str(raw.get("id", "")), name=str(raw.get("name", ""))
                )
            )
        else:
            logger.warning("Ignoring unsupported Anthropic content block type %r", kind)
        return out

    def handle(self, event: dict[str, Any]) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        event_type = event.get("type")
        if event_type == "message_start":
            self._load_message(_as_dict(event.get("message")) or {})
        elif event_type == "content_block_start":
            block = _as_dict(event.get("content_block")) or {}
            out.extend(self._start_block(_as_index(event), block))
        elif event_type == "content_block_delta":
            delta = _as_dict(event.get("delta")) or {}
            delta_type = _as_str(delta.get("type")) or ""
            index = _as_index(event)
            state = self._blocks.get(index)
            if state is None:
                logger.warning("Ignoring delta for unknown content block index %d", index)
            elif delta_type == "text_delta":
                text = str(delta.get("text", ""))
                state.raw["text"] = str(state.raw.get("text", "")) + text
                out.append(StreamTextDelta(text=text))
            elif delta_type == "thinking_delta":
                thinking = str(delta.get("thinking", ""))
                state.raw["thinking"] = str(state.raw.get("thinking", "")) + thinking
                out.append(StreamReasoningDelta(text=thinking))
            elif delta_type == "signature_delta":
                # Fragments of one signature; the whole of it is the block's state, emitted when
                # the block stops.
                signature = str(delta.get("signature", ""))
                state.raw["signature"] = str(state.raw.get("signature", "")) + signature
            elif delta_type == "input_json_delta":
                partial = str(delta.get("partial_json", ""))
                state.json_parts.append(partial)
                ordinal = self._tool_ordinals.get(index)
                # A server-side tool block (server_tool_use, mcp_tool_use) is not a tool call the
                # harness runs; its input exists only in the raw view.
                if ordinal is not None:
                    out.append(StreamToolUseInputDelta(index=ordinal, partial_json=partial))
            elif delta_type == "citations_delta":
                citations = state.raw.get("citations")
                if not isinstance(citations, list):
                    citations = []
                    state.raw["citations"] = citations
                cast("list[Any]", citations).append(copy.deepcopy(delta.get("citation")))
            elif delta_type not in _warned_delta_types:
                _warned_delta_types.add(delta_type)
                logger.warning("Ignoring unsupported Anthropic delta type %r", delta_type)
        elif event_type == "message_delta":
            if self._message is None:
                self._message = {}
            for key, value in (_as_dict(event.get("delta")) or {}).items():
                self._message[key] = copy.deepcopy(value)
            usage = _as_dict(event.get("usage"))
            if usage is not None:
                self._merge_usage(usage)
        elif event_type == "content_block_stop":
            state = self._blocks.get(_as_index(event))
            if state is not None and state.raw.get("type") == "thinking":
                signed = _signature_state(state.raw)
                if signed is not None:
                    out.append(StreamReasoningState(state=signed))
        # message_stop and ping carry nothing to assemble.
        return out

    def _stop(self) -> StopReason:
        message = self._message or {}
        stop_reason = _as_str(message.get("stop_reason"))
        if stop_reason == "end_turn":
            return StopEndTurn()
        if stop_reason == "max_tokens":
            return StopMaxTokens()
        if stop_reason == "stop_sequence":
            return StopStopSequence(stop_sequence=_as_str(message.get("stop_sequence")) or "")
        if stop_reason == "tool_use":
            return StopToolUse()
        if stop_reason == "refusal":
            # The model's refusal or Anthropic's classifiers: the API does not say which.
            return StopRefusal(
                provider_reason="refusal",
                complete=None,
                detail=cast("JSONObject | None", _as_dict(message.get("stop_details"))),
            )
        return StopOther(provider_reason=stop_reason or "no stop_reason reported")

    def _tool_input(self, state: _BlockState) -> tuple[dict[str, Any], str | None]:
        if state.json_parts:
            return _parse_tool_input("".join(state.json_parts))
        return copy.deepcopy(_as_dict(state.raw.get("input")) or {}), None

    def _content(self, state: _BlockState) -> Content | None:
        raw = state.raw
        kind = raw.get("type")
        if kind == "text":
            return ContentText(text=str(raw.get("text", "")))
        if kind == "thinking":
            return ContentReasoning(
                text=str(raw.get("thinking", "")),
                form="summary",
                reasoning_state=_signature_state(raw),
            )
        if kind == "redacted_thinking":
            return ContentReasoning(text="", form="redacted", reasoning_state=_redacted_state(raw))
        if kind == "tool_use":
            parsed, parse_error = self._tool_input(state)
            return ContentToolUse(
                id=str(raw.get("id", "")),
                name=str(raw.get("name", "")),
                input=parsed,
                parse_error=parse_error,
            )
        return None

    def _raw_block(self, state: _BlockState) -> dict[str, Any]:
        raw = copy.deepcopy(state.raw)
        if state.json_parts:
            raw["input"], _ = self._tool_input(state)
        return raw

    def finish(self) -> ModelResponse:
        if self._message is None:
            raise ModelError("Anthropic stream ended before a message_start event")
        content: list[Content] = []
        for index in self._block_order:
            block = self._content(self._blocks[index])
            if block is not None:
                content.append(block)
        details = _as_dict(self._usage.get("output_tokens_details")) or {}
        usage = Usage(
            input_tokens=_as_int(self._usage.get("input_tokens")) or 0,
            cache_read_input_tokens=_as_int(self._usage.get("cache_read_input_tokens")),
            cache_write_input_tokens=_as_int(self._usage.get("cache_creation_input_tokens")),
            output_tokens=_as_int(self._usage.get("output_tokens")) or 0,
            reasoning_output_tokens=_as_int(details.get("thinking_tokens")),
            speed=_speed_tier(self._usage.get("speed")),
        )
        return ModelResponse(
            id=str(self._message.get("id") or ""),
            output=(AssistantMessage(content=tuple(content)),),
            stop_reason=self._stop(),
            usage=usage,
        )

    def raw_message(self) -> dict[str, Any] | None:
        """The message as the frames described it, in the API's own `Message` shape."""
        if self._message is None:
            return None
        return {
            **copy.deepcopy(self._message),
            "content": [self._raw_block(self._blocks[index]) for index in self._block_order],
            "usage": copy.deepcopy(self._usage),
        }


def _parse_tool_input(raw: str) -> tuple[dict[str, Any], str | None]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return {}, str(exc)
    if not isinstance(parsed, dict):
        return {}, "tool call arguments decoded to a non-object JSON value"
    return cast(dict[str, Any], parsed), None


_ERROR_TYPE_CATEGORIES: dict[str, FailureCategory] = {
    "overloaded_error": "overloaded",
    "rate_limit_error": "rate_limited",
    "authentication_error": "authentication",
    "permission_error": "authentication",
    "invalid_request_error": "invalid_request",
    "not_found_error": "invalid_request",
    "api_error": "server_error",
}


def failure_category_for_status(status: int, error_text: str) -> FailureCategory:
    """The failure category for an Anthropic error status, disambiguated by the error text."""
    lower = error_text.lower()
    if status == 400 and ("prompt is too long" in lower or "too many tokens" in lower):
        return "context_length"
    if status == 400 and "content filtering" in lower:
        return "content_filter"
    if status in (401, 403):
        return "authentication"
    if status == 429:
        return "rate_limited"
    if status == 529 or "overloaded" in lower:
        return "overloaded"
    if 400 <= status < 500:
        return "invalid_request"
    return "server_error"


def _error_fields(payload: dict[str, Any]) -> tuple[str, str]:
    """(error type, message) of an Anthropic error object, tolerating either shape."""
    error = _as_dict(payload.get("error")) or payload
    return str(error.get("type") or ""), str(error.get("message") or "")


def _failure_from_error_event(payload: dict[str, Any]) -> ModelCallFailure:
    """An in-stream `error` event (which carries no HTTP status) as a failure."""
    error_type, message = _error_fields(payload)
    category = _ERROR_TYPE_CATEGORIES.get(error_type) or failure_category_for_status(500, message)
    return ModelCallFailure(
        category=category,
        description=f"{error_type}: {message}"[:_MAX_FAILURE_DESCRIPTION],
        provider_http_status=None,
    )


class AnthropicSseDecoder:
    """Decodes an Anthropic Messages SSE stream into stream events and the assembled response.
    Unknown event and delta types (pings, beta progress deltas) are tolerated and ignored; an
    in-stream `error` event ends the stream as a failure."""

    def __init__(self) -> None:
        self._frames = SseFrameParser()
        self._assembler = _MessageAssembler()
        self._failure: ModelCallFailure | None = None
        self._finished = False
        self._stopped = False

    def _consume(self, frames: list[tuple[str | None, str]]) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for _event, data in frames:
            if self._failure is not None:
                continue
            payload = decode_json_object(data, "Anthropic stream event")
            if payload.get("type") == "error":
                self._failure = _failure_from_error_event(payload)
                continue
            if payload.get("type") == "message_stop":
                self._stopped = True
            events.extend(self._assembler.handle(payload))
        return events

    def complete(self) -> bool:
        return self._stopped or self._failure is not None

    def feed(self, chunk: bytes) -> list[StreamEvent]:
        return self._consume(self._frames.feed(chunk))

    def finish(self) -> ModelResponse | ModelCallFailure:
        self._consume(self._frames.finish())
        self._finished = True
        if self._failure is not None:
            return self._failure
        try:
            return self._assembler.finish()
        except ModelError as exc:
            return ModelCallFailure(
                category="server_error",
                description=f"Anthropic stream ended incomplete: {exc}",
                provider_http_status=None,
            )

    def raw_message(self) -> JSONObject | None:
        if not self._finished or self._failure is not None:
            return None
        return cast("JSONObject", self._assembler.raw_message())


def decode_anthropic_message(body: bytes) -> ModelResponse | ModelCallFailure:
    """A complete (non-streamed) Anthropic `Message` body as a ModelResponse."""
    payload = decode_json_object(body, "Anthropic message body")
    if payload.get("type") == "error":
        return _failure_from_error_event(payload)
    try:
        return _MessageAssembler.from_message(payload).finish()
    except ModelError as exc:
        return ModelCallFailure(
            category="server_error", description=str(exc), provider_http_status=None
        )


def decode_anthropic_failure(status: int, body: bytes) -> ModelCallFailure:
    """A non-success response as a failure, classified by status and error text."""
    text = body.decode("utf-8", errors="replace")
    description = text[:_MAX_FAILURE_DESCRIPTION]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        error_type, message = _error_fields(cast(dict[str, Any], parsed))
        if message:
            description = f"{error_type}: {message}"[:_MAX_FAILURE_DESCRIPTION]
    return ModelCallFailure(
        category=failure_category_for_status(status, description),
        description=description,
        provider_http_status=status,
    )


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_index(event: dict[str, Any]) -> int:
    index = event.get("index")
    return index if isinstance(index, int) and not isinstance(index, bool) else 0
