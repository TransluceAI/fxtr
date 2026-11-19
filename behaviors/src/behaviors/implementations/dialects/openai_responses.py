"""OpenAI Responses `/v1/responses` ⇄ behaviors model vocabulary translation: the
`OpenAIResponsesCodec`.

Pure functions and a codec object binding them to the `DialectCodec` protocol; no I/O and no
third-party imports. This is the dialect Codex speaks (its only supported `wire_api` is
`"responses"`). The harness side (serving Codex) is documented here; the
model side (`OpenAIResponsesAPI` calling OpenAI or a compatible Responses server:
`encode_responses_request`, `ResponsesSseDecoder`, `decode_responses_response`,
`decode_responses_failure`) is documented at its own section further down. Harness side:

* **Request** (`responses_request_to_canonical`): a Responses API request body → a
  `DecodedRequest`. The top-level `instructions` prompt is folded into a leading
  `SystemMessage`; each `function_call` / `function_call_output` input item becomes its own
  assistant / tool message, in input order, and a `reasoning` item is folded into the assistant
  message of the `function_call` or assistant `message` that follows it (they were one model
  response).
* **Response**: `synthesize_responses_sse` turns a subject-model stream (`StreamEvent` deltas plus
  the terminal `ModelResponse` or `ModelCallFailure`) into the Responses streaming SSE event
  sequence (`response.created` → per-item `response.output_item.*` / `response.output_text.delta`
  / `response.function_call_arguments.delta` → `response.completed`);
  `response_to_responses_response` builds the non-streaming `Response` body.
  `responses_failure_body` renders a request-time `ModelCallFailure` as the dialect's status +
  error body.

Client-compatibility notes (verified against the Codex CLI's Responses consumer,
`openai/codex` `codex-rs/codex-api/src/sse/responses.rs` @ rust-v0.146.0, and exercised live
at 0.160.1 including the `custom_tool_call` item and its input events):

* The authoritative output is the complete `ResponseItem` carried by each
  `response.output_item.done` event — the `*.delta` events are display-only to that client. Both
  are synthesized here.
* `response.completed` must carry `response.id` and integer `usage` totals (or `usage: null`);
  the client does not require the full `output` array to be echoed, but it is included for
  faithfulness to the real API.
* A `StopMaxTokens` stop reason is rendered as `response.incomplete` with
  `incomplete_details.reason: "max_output_tokens"`, matching the real API (the Codex client
  surfaces it as a stream error, which is also what it does against the real endpoint).

Request coverage: what the decoder does with each field the request schema allows (models it, keeps
it as an extra for a same-dialect subject, or drops it with a recorded loss), and how the
reconstruction check compares it, is tabulated per schema path in `coverage/openai_responses.yaml`.
The table is generated from the pinned SDK's request schema by `tests/dialect_coverage`, and a test
fails when the codec stops matching it. The notes below cover what it cannot show: the response
side, and the design choices behind some of its rows.

Known fidelity weak spots:

* **Reasoning is rendered whole, not streamed live.** `response.output_item.added` names the
  item's id, and for OpenAI's own reasoning state that is the provider's id, which arrives with
  the state after the text; so a reasoning block is held and its item emitted complete when the
  block ends. OpenAI's state (`OpenAIResponsesReasoning`) goes out as issued: its id (a synthetic
  `rs_behaviors…` one when the item had none, elided again on ingest) and `encrypted_content`.
  Any other reasoning, another provider's state or none at all (literal reasoning, an unsigned
  summary), rides `encrypted_content` in an envelope (`_reasoning_envelope`) under a synthetic
  id, its text in `summary`: Codex re-sends `summary` and `encrypted_content` on continuations
  but strips `reasoning_text` content (`should_serialize_reasoning_content` in its protocol
  crate). Ingest reads an envelope back as the block it carried, and any other
  `encrypted_content` as OpenAI's state.
* **Tool-call arguments are re-encoded.** The assembled `ContentToolUse.input` is a parsed
  object, so non-streaming `arguments` strings are `json.dumps` of it, not the model's original
  byte sequence (streamed `arguments` deltas are the raw fragments); a call whose arguments never
  parsed (`parse_error`) renders as `"{}"`.
* **Custom (freeform) tools, namespaces and mid-conversation additions.** A `custom` tool (one
  free-text input constrained by a grammar; Codex's `apply_patch`) is a `ToolParam` whose
  `input_schema` is the one-field object `{"input": string}` and whose `freeform_format` is the
  tool's `format`, filled in as `{"type": "text"}` when the request left it absent; a call to it
  (`custom_tool_call`) is a `ContentToolUse` with `input={"input": text}`. A `namespace` entry
  flattens to its member tools, each carrying the group as `ToolParam.namespace`, and a call to
  one carries `ContentToolUse.namespace`. Tools declared in an `additional_tools` input item join
  the request's tool list. Rendering back to this dialect regroups namespaces and emits
  `custom_tool_call` items (and `response.custom_tool_call_input.*` events) for calls to freeform
  tools, looked up in the request's declarations; a call named by a dialect without namespaces
  is resolved through the same table (`_common.ToolNames`).
* **A message's `phase` is not carried.** Current OpenAI models tag each message with the channel
  Codex shows it on (`commentary` for a preamble, `final_answer` for the answer); the vocabulary
  has no slot for it, so a subject's messages reach a Codex harness without one.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from behaviors.implementations.dialects._common import (
    BodyReader,
    LossLog,
    ReasoningStateFilter,
    ResolvedCall,
    ToolNames,
    apply_declared_defaults,
    apply_dialect_extras,
    failure_message,
    merge_provider_params,
    normalized_path,
    path_matches,
    request_extras,
    unadapted,
    without_reasoning,
)
from behaviors.implementations.dialects._openai_common import (
    budget_as_effort,
    decode_failure_body,
    failure_from_error_object,
    reasoning_effort,
    response_format_flat,
    response_format_from_flat,
    service_tier_param,
    service_tier_speed,
)
from behaviors.implementations.dialects._reasoning_envelope import (
    envelope_text,
    read_text_slot,
)
from behaviors.implementations.dialects.adaptation import (
    AdaptedRequestBuilder,
    clear_tool_error_flags,
    drop_assistant_blocks,
    drop_breakpoints,
    drop_foreign_extras,
    drop_foreign_reasoning,
    drop_stop_sequences,
    fit_cache_ttls,
    merge_tool_result_breakpoints,
    tool_results_first,
)
from behaviors.implementations.dialects.codec_utils import (
    SseFrameParser,
    decode_json_object,
    own_reasoning_state,
)
from behaviors.implementations.dialects.contract_adaptation import (
    drop_non_object_response_format,
    move_rejected_keywords,
    move_strict_rejected_keywords,
    strict_problems,
    unenforce_incompatible_response_format,
    unenforce_incompatible_strict_tools,
)
from behaviors.implementations.dialects.schema_support import openai_rejects
from behaviors.interfaces.dialect_codecs import (
    AdaptedRequest,
    CodecError,
    DecodedRequest,
    Route,
    SseDecoder,
    StopRendering,
)
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
    SamplingParams,
    SpeedTier,
    StopContentFilter,
    StopEndTurn,
    StopMaxTokens,
    StopOther,
    StopReason,
    StopRefusal,
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
class OpenAIResponsesReasoning(
    TaggedDataclass, fxtr_type=behaviors_type("OpenAIResponsesReasoning.v1")
):
    """An OpenAI Responses `reasoning` item's identity: its id and `encrypted_content`, which the
    API verifies against each other.

    Attributes:
        id: The item's id, or None for an item that arrived without one (a Codex session resumed
            from its files sends those). The API accepts such an item without an id, since the
            payload names its own item, and rejects one sent under an id the payload does not name.
        encrypted_content: The encrypted reasoning.
    """

    id: str | None
    encrypted_content: str


logger = logging.getLogger(__name__)

SYNTHETIC_REASONING_ID_PREFIX = "rs_behaviors"
"""Prefix of the item ids given to reasoning items that carry no id of their own: reasoning
rendered to a harness in an envelope, or OpenAI state whose item had none. A decoder recognizes
the prefix and drops the id again, so it never reaches a provider."""

# --- request: Responses body -> DecodedRequest ----------------------------------------------------


def responses_request_to_canonical(
    body: dict[str, Any], headers: Headers = (), target: str = ""
) -> DecodedRequest:
    """Translate a parsed Responses API request into a `DecodedRequest`.

    What the vocabulary has no slot for is attached as `DialectExtras` (`ModelRequest.extras`):
    the leftover leaves of `reasoning` (`summary`, an effort the neutral vocabulary lacks),
    the leftover leaves of `text` (`verbosity`, a format the vocabulary cannot carry whole),
    every top-level key the decoder does not read (`store`, `include`, `prompt_cache_key`,
    `service_tier`, `metadata`, ...), feature-flag and unknown headers, and the query.
    """
    losses = LossLog()
    # Codex declares its newer models' tools inside the input as well as at the top level.
    added_tools = [
        tool
        for item in _dict_list(body.get("input"))
        if item.get("type") == "additional_tools"
        for tool in _dict_list(item.get("tools"))
    ]
    reader = BodyReader(body)
    parallel = reader.take("parallel_tool_calls")
    context = _convert_input(reader.take("input"), reader.take("instructions"), losses)
    tools = _convert_tools(reader.take("tools"), losses)
    tools.extend(_convert_tools(added_tools, losses))
    tool_choice, tools = _convert_tool_choice(reader.take("tool_choice"), tools, losses)
    sampling = SamplingParams(
        max_tokens=_as_int(reader.take("max_output_tokens")),
        temperature=_as_float(reader.take("temperature")),
        top_p=_as_float(reader.take("top_p")),
        reasoning=_convert_reasoning(reader),
        speed=_convert_speed(reader),
    )
    model = reader.take("model")
    streaming = bool(reader.take("stream") or False)
    request = ModelRequest(
        context=tuple(context),
        tools=tuple(tools),
        tool_choice=tool_choice,
        parallel_tool_calls=parallel if isinstance(parallel, bool) else None,
        sampling=sampling,
        response_format=_convert_text_format(reader),
        extras=request_extras("openai_responses", reader, headers, target),
    )
    return DecodedRequest(
        model=str(model if model is not None else ""),
        request=request,
        streaming=streaming,
        decode_losses=losses.recorded(),
    )


# Tool `type`s the API defines but the harness runs (it executes the call and returns the
# result through the matching `*_output` item): not provider-side, and not `ToolParam`s either.
_CLIENT_TOOL_TYPES = frozenset({"computer_use_preview", "computer", "local_shell", "apply_patch"})
# `tool_search` runs at the provider (or the client), but only over the request's own (deferred)
# tool definitions: no reach beyond the request, so not an egress path either. Codex declares it.
_NO_REACH_TOOL_TYPES = frozenset({"tool_search"})
# Where the `shell` tool runs is its `environment`: `local` is the harness, the container kinds
# are the provider's own sandbox, which has network access of its own.
_HOSTED_SHELL_ENVIRONMENTS = frozenset({"container_auto", "container_reference"})

# A tool call and its result, in both the function and the freeform (custom) form.
_CALL_TYPES = frozenset({"function_call", "custom_tool_call"})
_CALL_OUTPUT_TYPES = frozenset({"function_call_output", "custom_tool_call_output"})
# How a freeform (`custom`) tool looks in the model vocabulary: one string field, `input`.
_CUSTOM_TOOL_SCHEMA: JSONObject = {
    "type": "object",
    "properties": {"input": {"type": "string"}},
    "required": ["input"],
}

# Input item types that are a provider-side tool's call or result, echoed back in the history.
_PROVIDER_TOOL_ITEM_TYPES = frozenset(
    {
        "web_search_call",
        "file_search_call",
        "code_interpreter_call",
        "image_generation_call",
        "mcp_call",
        "mcp_list_tools",
        "mcp_approval_request",
        "mcp_approval_response",
    }
)
# Input item types of the client-run API-defined tools above.
_CLIENT_TOOL_ITEM_TYPES = frozenset(
    {
        "computer_call",
        "computer_call_output",
        "local_shell_call",
        "local_shell_call_output",
        "shell_call",
        "shell_call_output",
        "apply_patch_call",
        "apply_patch_call_output",
    }
)


def _provider_tool_family(tool: dict[str, Any]) -> ProviderToolFamily | None:
    """The family of one declared tool, or None for a tool the harness runs (or one with no
    reach beyond the request). A `shell` is classified by where it executes; a shell whose
    environment is missing or unrecognized is `unknown`, so it fails closed."""
    kind = tool.get("type")
    if not isinstance(kind, str) or kind in ("function", "custom", "namespace"):
        return None
    if kind.startswith("web_search"):
        return "web_search"
    if kind == "code_interpreter":
        return "code_execution"
    if kind == "file_search":
        return "file_search"
    if kind == "image_generation":
        return "image_generation"
    if kind == "mcp":
        return "mcp"
    if kind == "shell":
        environment = _as_dict(tool.get("environment"))
        where = environment.get("type") if environment is not None else None
        if where == "local":
            return None
        if where in _HOSTED_SHELL_ENVIRONMENTS:
            return "code_execution"
        return "unknown"
    if kind in _CLIENT_TOOL_TYPES or kind in _NO_REACH_TOOL_TYPES:
        return None
    return "unknown"


def _provider_tool_label(tool: dict[str, Any], family: ProviderToolFamily) -> str:
    kind = str(tool.get("type"))
    if family == "mcp" and isinstance(tool.get("server_label"), str):
        return f"mcp:{tool['server_label']}"
    if kind == "shell":
        environment = _as_dict(tool.get("environment"))
        where = environment.get("type") if environment is not None else None
        return f"shell:{where}"
    return kind


def _declared_tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Every tool declaration in a request, from every place the dialect accepts one: the
    top-level `tools`, the `tools` of each `namespace` entry, and the `tools` of each
    `additional_tools` input item (a mid-conversation addition), namespaces included."""
    found: list[dict[str, Any]] = []

    def visit(tools: Any) -> None:
        for tool in _dict_list(tools):
            found.append(tool)
            if tool.get("type") == "namespace":
                visit(tool.get("tools"))

    visit(body.get("tools"))
    items = body.get("input")
    for item in _dict_list(items) if isinstance(items, list) else []:
        if item.get("type") == "additional_tools":
            visit(item.get("tools"))
    return found


def provider_tools_in_request(body: dict[str, Any]) -> tuple[ProviderTool, ...]:
    """The provider-side tools a `/v1/responses` body declares anywhere (`_declared_tools`),
    classified by `_provider_tool_family`."""
    out: list[ProviderTool] = []
    for tool in _declared_tools(body):
        family = _provider_tool_family(tool)
        if family is not None:
            out.append(ProviderTool(family=family, name=_provider_tool_label(tool, family)))
    return tuple(out)


def _note_skipped_tool(tool: dict[str, Any], losses: LossLog) -> None:
    """Record a declared tool the decoder does not turn into a `ToolParam`."""
    kind = tool.get("type")
    family = _provider_tool_family(tool)
    if family is not None:
        losses.note(
            f"provider_tool:{family}", f"provider-side tool {_provider_tool_label(tool, family)}"
        )
    elif kind in _NO_REACH_TOOL_TYPES:
        losses.note("tool_search", "the tool_search tool")
    else:
        losses.note(
            "builtin_tool",
            f"API-defined tool the harness runs: {_provider_tool_label(tool, 'unknown')}",
        )


# The dialect's effort levels are the neutral vocabulary itself: OpenAI's standard four, Codex's
# `xhigh` above them, and the `max` the 5.6 family accepts. "none" is reasoning explicitly off.
_EFFORT_LEVELS: tuple[str, ...] = get_args(ReasoningEffort)


def _convert_text_format(reader: BodyReader) -> ResponseFormat | None:
    fmt = _as_dict(reader.peek("text", "format"))
    if fmt is None:
        return None
    response_format = response_format_from_flat(fmt)
    if response_format is not None:
        reader.take_leaf("text", "format")
    return response_format


def _convert_speed(reader: BodyReader) -> SpeedTier | None:
    """`service_tier` as a neutral speed tier (see `service_tier_speed`); a tier the vocabulary
    does not model stays in the extras."""
    speed = service_tier_speed(reader.peek("service_tier"))
    if speed is not None:
        reader.take("service_tier")
    return speed


def _convert_reasoning(reader: BodyReader) -> ReasoningParams | None:
    """The request's `reasoning` config as neutral reasoning params.

    A `reasoning` object means reasoning is on; `effort` is consumed only when it maps to a
    neutral level (an unknown one stays in the extras, for the provider to validate). A
    `summary` mode means summaries are wanted (`summary=True`); the mode the vocabulary spells
    (`detailed`) is consumed, the others (`auto`, `concise`) stay in the extras beside the
    choice, so a same-dialect subject gets the mode verbatim.
    """
    if not isinstance(reader.peek("reasoning"), dict):
        return None
    effort = reader.peek("reasoning", "effort")
    if effort == "none":
        reader.take_leaf("reasoning", "effort")
        return ReasoningParams(enabled=False)
    summary = _take_summary(reader)
    if isinstance(effort, str) and effort in _EFFORT_LEVELS:
        reader.take_leaf("reasoning", "effort")
        return ReasoningParams(effort=cast("ReasoningEffort", effort), summary=summary)
    return ReasoningParams(effort=None, summary=summary)


_SUMMARY_MODES = frozenset({"auto", "concise", "detailed"})
_NEUTRAL_SUMMARY_MODE = "detailed"


def _take_summary(reader: BodyReader) -> bool | None:
    """`reasoning.summary` as the neutral choice. An absent key is no choice; an explicit null
    is the opt-out (summaries are opt-in at this API) and stays in the extras as that exact
    spelling; a mode is the opt-in, consumed when it is the mode the vocabulary spells
    (`detailed`) and kept as an extra otherwise (`auto`, `concise`); an unknown value is no
    choice and stays for the provider to validate."""
    reasoning = _as_dict(reader.peek("reasoning"))
    if reasoning is None or "summary" not in reasoning:
        return None
    mode = reasoning["summary"]
    if mode is None:
        return False
    if not isinstance(mode, str) or mode not in _SUMMARY_MODES:
        return None
    if mode == _NEUTRAL_SUMMARY_MODE:
        reader.take_leaf("reasoning", "summary")
    return True


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


def _text_message(role: str, text: str) -> ChatMessage:
    content = (ContentText(text=text),)
    if role == "assistant":
        return AssistantMessage(content=content)
    if role == "system":
        return SystemMessage(content=content)
    return UserMessage(content=content)


def _convert_input(input_value: Any, instructions: Any, losses: LossLog) -> list[ChatMessage]:
    """Fold `instructions` + the `input` item list into context messages.

    Chat-shaped providers require each tool message to *immediately* follow the assistant
    message carrying its call, while Responses input orders items however the client accumulated
    them (Codex groups parallel calls as call, call, output, output). So each
    `function_call_output` is emitted right after its `function_call` (matched by `call_id`)
    rather than at its own position; an output whose call never appears stays in place. A
    `reasoning` item joins the assistant message of the next `function_call` or assistant
    `message`, as it did in the model response that produced them.
    """
    out: list[ChatMessage] = []
    if isinstance(instructions, str) and instructions:
        out.append(_text_message("system", instructions))
    if isinstance(input_value, str):
        out.append(_text_message("user", input_value))
        return out
    items = _dict_list(input_value)
    outputs: dict[str, ToolMessage] = {}
    for item in items:
        if item.get("type") in _CALL_OUTPUT_TYPES:
            if not isinstance(item.get("call_id"), str):
                # The vocabulary pairs a result with its call by id; one without an id answers
                # no call a translated history can name.
                losses.count("unpaired_tool_output", "tool output item(s) without a call_id")
            call_id = str(item.get("call_id") or "")
            outputs.setdefault(
                call_id,
                ToolMessage(
                    content=_output_blocks(item.get("output"), losses),
                    tool_call_id=call_id,
                    is_error=None,
                ),
            )
    consumed: set[str] = set()
    # Reasoning blocks waiting for the assistant item they preceded in the model's response.
    pending_reasoning: list[Content] = []

    def flush_reasoning() -> None:
        if pending_reasoning:
            out.append(AssistantMessage(content=tuple(pending_reasoning)))
            pending_reasoning.clear()

    # Whether every item so far is an instruction (system or developer) message.
    leading = True
    for item in items:
        item_type = item.get("type", "message")  # untyped input items are messages
        instruction = item_type == "message" and item.get("role") in ("system", "developer")
        if instruction and not leading and item.get("role") == "system":
            # The API's two instruction roles; the vocabulary has one system message, rendered
            # back as `developer`. Before the conversation they carry the same instructions,
            # but whether a later one carries the same authority as either is not documented.
            losses.count("system_role", "system item(s) after the conversation began, as developer")
        if not instruction and item_type != "additional_tools":
            leading = False
        if item_type in _CALL_TYPES or item_type in _CALL_OUTPUT_TYPES:
            _note_call_context(item, losses)
        if item_type == "message" and "role" not in item and "id" in item:
            # An untyped `{id}` is an item reference (to an item the API stored), not a message.
            losses.count("item:item_reference", "'item_reference' input item(s)")
        elif item_type == "message":
            converted = _convert_message_item(item, losses)
            if isinstance(converted, AssistantMessage):
                converted = AssistantMessage(content=(*pending_reasoning, *converted.content))
                pending_reasoning.clear()
            else:
                flush_reasoning()
            if converted is not None:
                out.append(converted)
        elif item_type in _CALL_TYPES:
            call_id = str(item.get("call_id", ""))
            if item_type == "custom_tool_call":
                # A freeform call: its input is one string (see the module docstring).
                input_object: JSONObject = {"input": str(item.get("input", ""))}
                parse_error = None
            else:
                input_object, parse_error = _parse_arguments(str(item.get("arguments", "")))
            namespace = item.get("namespace")
            tool_use = ContentToolUse(
                id=call_id,
                name=str(item.get("name", "")),
                input=input_object,
                parse_error=parse_error,
                namespace=namespace if isinstance(namespace, str) else None,
            )
            out.append(AssistantMessage(content=(*pending_reasoning, tool_use)))
            pending_reasoning.clear()
            if call_id in outputs and call_id not in consumed:
                consumed.add(call_id)
                out.append(outputs[call_id])
        elif item_type in _CALL_OUTPUT_TYPES:
            flush_reasoning()
            call_id = str(item.get("call_id") or "")
            if call_id not in consumed and not any(
                later.get("type") in _CALL_TYPES and str(later.get("call_id", "")) == call_id
                for later in items
            ):
                # An orphan output (its call never appears in the input): keep it in place.
                consumed.add(call_id)
                out.append(outputs[call_id])
                if item_type == "custom_tool_call_output":
                    # Without its call, nothing in the request says the result answered a
                    # freeform call, and an encoder re-rendering this history gives it the
                    # function result's shape.
                    losses.note(
                        "orphan_freeform_result",
                        f"result {call_id} of a freeform call this request does not carry",
                    )
        elif item_type == "reasoning":
            block = _convert_reasoning_block(item)
            if block is not None:
                pending_reasoning.append(block)
            elif not item.get("encrypted_content"):
                losses.count("unsigned_reasoning", "reasoning item(s) without encrypted_content")
        elif item_type == "additional_tools":
            pass  # its declarations joined the tool list in `responses_request_to_canonical`
        else:
            _count_dropped_item(item, losses)
    flush_reasoning()
    return out


def _note_call_context(item: dict[str, Any], losses: LossLog) -> None:
    """Record how a call or its output was made, where the vocabulary has only the model's own
    blocking calls: one a program made (programmatic tool calling, whose code runs at the
    provider), or an async call the model did not wait for. A `direct` caller is the default."""
    caller = _as_dict(item.get("caller"))
    if caller is not None and caller.get("type") != "direct":
        losses.count("programmatic_tool_calling", "tool call/output item(s) made by a program")
    if item.get("async") is True:
        losses.count("async_tool_calling", "async tool call item(s)")


def _count_dropped_item(item: dict[str, Any], losses: LossLog) -> None:
    """Record an input item the vocabulary has no slot for, at the point it is dropped."""
    item_type = item.get("type")
    if item_type in _PROVIDER_TOOL_ITEM_TYPES:
        losses.count(
            "provider_tool_exchange", "provider-side tool call/result item(s) in the history"
        )
    elif item_type in _CLIENT_TOOL_ITEM_TYPES:
        losses.count("builtin_tool", "API-defined tool call/output item(s) in the history")
    elif item_type == "tool_search_call":
        losses.count("tool_search", "tool_search call item(s) in the history")
    else:
        losses.count(f"item:{item_type}", f"'{item_type}' input item(s)")


def _convert_reasoning_block(item: dict[str, Any]) -> ContentReasoning | None:
    """A `reasoning` input item as a content block, or None for one with nothing to echo.

    `encrypted_content` holds either an envelope (reasoning this dialect carried for another
    provider, or without any state) or OpenAI's own payload, which with the item's id is the
    block's `OpenAIResponsesReasoning` (see `_reasoning_envelope`). An item without
    `encrypted_content` has nothing a provider could verify and is dropped.
    """
    encrypted = item.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted:
        return None
    summary, literal = _reasoning_texts(item)
    raw_id = item.get("id")
    item_id = (
        raw_id
        if isinstance(raw_id, str)
        and raw_id
        and not raw_id.startswith(SYNTHETIC_REASONING_ID_PREFIX)
        else None
    )
    echoed = read_text_slot(
        encrypted, lambda raw: OpenAIResponsesReasoning(id=item_id, encrypted_content=raw)
    )
    text = summary or literal
    if echoed.state is None and not text and echoed.form != "redacted":
        return None
    form = echoed.form or _reasoning_form(summary, literal)
    return ContentReasoning(text=text, form=form, reasoning_state=echoed.state)


def _reasoning_texts(item: dict[str, Any]) -> tuple[str, str]:
    """A `reasoning` item's summary text and its literal (`content`) text."""
    summary = "".join(
        str(part.get("text", ""))
        for part in _dict_list(item.get("summary"))
        if part.get("type") == "summary_text"
    )
    literal = "".join(
        str(part.get("text", ""))
        for part in _dict_list(item.get("content"))
        if part.get("type") == "reasoning_text"
    )
    return summary, literal


def _reasoning_form(summary: str, literal: str) -> ReasoningForm:
    """The form of an item's text: the summary when it has one (it is the text kept), the
    literal reasoning when that is all it has, and an empty summary when it has neither."""
    return "full" if literal and not summary else "summary"


def _parse_arguments(raw: str) -> tuple[JSONObject, str | None]:
    """A Responses `arguments` string as a parsed input object (empty string parses to `{}`)."""
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, str(exc)
    data = _as_dict(parsed)
    if data is None:
        return {}, "tool call arguments decoded to a non-object JSON value"
    return cast("JSONObject", data), None


def _convert_message_item(item: dict[str, Any], losses: LossLog) -> ChatMessage | None:
    role = item.get("role")
    if role == "assistant":
        return AssistantMessage(content=_assistant_parts(item.get("content"), losses))
    if role in ("system", "developer"):
        # The Responses API's "developer" role is its system-tier role; both fold to system.
        return SystemMessage(content=_message_parts(item.get("content"), losses))
    if role == "user":
        return UserMessage(content=_message_parts(item.get("content"), losses))
    return None


def _message_parts(content: Any, losses: LossLog) -> tuple[ContentText, ...]:
    """Message content (a string or a list of `input_text`/`output_text` parts) as one text block
    per part, so a multi-part message (Codex's developer preamble is two parts) renders back with
    the same parts; every other part is recorded as dropped."""
    return _content_parts(content, losses, text_types=("input_text", "output_text"))


def _assistant_parts(content: Any, losses: LossLog) -> tuple[ContentText, ...]:
    """An assistant message's content: its text parts as `_message_parts` reads them, and its
    `refusal` parts as their text, in order. A refusal is a response's stop reason
    (`StopRefusal`), which a message in the history has no slot for, so that it was one is a
    recorded loss."""
    if isinstance(content, str):
        return (ContentText(text=content),)
    blocks: list[ContentText] = []
    for part in _dict_list(content):
        if part.get("type") == "refusal":
            losses.count("refusal", "refusal part(s) in the history, sent as text")
            blocks.append(ContentText(text=str(part.get("refusal", ""))))
        else:
            blocks.extend(_message_parts([part], losses))
    return tuple(blocks) or (ContentText(text=""),)


def _output_blocks(output: Any, losses: LossLog) -> tuple[ContentText, ...]:
    """A `function_call_output` payload (a plain string or a list of content items) as text
    blocks, one per part; every non-text item is recorded as dropped, through the same part
    handling as a message's."""
    return _text_blocks(
        _content_parts(output, losses, text_types=("input_text", "output_text", "text"))
    )


def _text_blocks(parts: tuple[ContentText, ...]) -> tuple[ContentText, ...]:
    """Text parts kept one block per part (an assistant message, a tool output), so a part's
    cache breakpoint keeps its place; an empty container is one empty block."""
    return parts or (ContentText(text=""),)


def _breakpoint(block: Content) -> CacheControl | None:
    return block.cache_control if isinstance(block, CacheableContentText) else None


def _content_parts(
    content: Any, losses: LossLog, *, text_types: tuple[str, ...]
) -> tuple[ContentText, ...]:
    """The one content-part decoder every container in the dialect delegates to (messages of
    any role, function-call outputs): text parts become `ContentText`, anything else is
    recorded on `losses` as it is dropped. An `output_text` part's `annotations` (citations)
    are a child the vocabulary cannot carry either."""
    if isinstance(content, str):
        return (ContentText(text=content),)
    parts: list[ContentText] = []
    for block in _dict_list(content):
        kind = block.get("type")
        if kind in text_types:
            if block.get("annotations"):
                losses.count("citations", "text part annotation list(s)")
            text = str(block.get("text", ""))
            # An omitted breakpoint TTL inherits `prompt_cache_options.ttl`, kept in extras.
            if (breakpoint := _as_dict(block.get("prompt_cache_breakpoint"))) is not None:
                ttl = breakpoint.get("ttl")
                if ttl is not None and not isinstance(ttl, str):
                    losses.count(
                        "cache_breakpoint", "breakpoint TTL(s) that are not strings", semantic=False
                    )
                    ttl = None
                parts.append(CacheableContentText(text=text, cache_control=CacheControl(ttl=ttl)))
            else:
                parts.append(ContentText(text=text))
        else:
            _count_dropped_part(kind, losses)
    return tuple(parts)


def _count_dropped_part(part_type: object, losses: LossLog) -> None:
    """Record a content part the vocabulary has no slot for, at the point it is dropped."""
    if part_type == "input_image":
        losses.count("image", "image part(s)")
    elif part_type == "input_audio":
        losses.count("audio", "audio part(s)")
    elif part_type == "input_file":
        losses.count("document", "file part(s)")
    else:
        losses.count(f"part:{part_type}", f"'{part_type}' part(s)")


def _convert_tools(
    tools: Any,
    losses: LossLog,
    *,
    namespace: str | None = None,
    namespace_description: str | None = None,
) -> list[ToolParam]:
    """Function and freeform (`custom`) tools, declared on their own or grouped under
    `namespace` entries (whose members carry the group's name and description). Every other
    declaration is a tool the vocabulary cannot carry and is recorded on `losses`."""
    out: list[ToolParam] = []
    deferred = 0
    for tool in _dict_list(tools):
        kind = tool.get("type")
        name = tool.get("name")
        if kind == "namespace":
            group_description = tool.get("description")
            out.extend(
                _convert_tools(
                    tool.get("tools"),
                    losses,
                    namespace=name if isinstance(name, str) else None,
                    namespace_description=(
                        group_description if isinstance(group_description, str) else None
                    ),
                )
            )
            continue
        if kind not in ("function", "custom"):
            _note_skipped_tool(tool, losses)
            continue
        if not isinstance(name, str):
            continue
        if tool.get("defer_loading") is True:
            deferred += 1
        if not _note_tool_options(name, tool, losses):
            continue
        description = tool.get("description")
        strict = tool.get("strict")
        custom = kind == "custom"
        out.append(
            ToolParam(
                name=name,
                description=description if isinstance(description, str) else None,
                # Responses function tools are flattened (name/parameters at the top level,
                # unlike the chat API's nested "function" object).
                input_schema=(
                    copy.deepcopy(_CUSTOM_TOOL_SCHEMA)
                    if custom
                    else cast("JSONObject", tool.get("parameters") or {})
                ),
                strict=strict if isinstance(strict, bool) else "auto",
                input_examples=None,
                namespace=namespace,
                namespace_description=namespace_description,
                freeform_format=(
                    (_as_dict(tool.get("format")) or {"type": "text"}) if custom else None
                ),
                output_schema=None if custom else _as_dict(tool.get("output_schema")),
            )
        )
    if deferred:
        # Deferred definitions are offered whole to a translated subject (it has no tool search).
        losses.note("tool_search", f"{deferred} tool definition(s) marked defer_loading")
    return out


def _note_tool_options(name: str, tool: dict[str, Any], losses: LossLog) -> bool:
    """Record what a function or custom tool declares beyond its name, description and input:
    who may call it (`["direct"]`, the model, is the default), whether the model waits for its
    result (`async`), and, for a freeform tool, the schema of its output. The vocabulary's
    `ToolParam` has no slot for these (a function tool's output schema is
    `ToolParam.output_schema`).
    Returns whether the model may call the tool itself: one callable only from code
    (programmatic tool calling) is left out rather than offered to the model."""
    if tool.get("async") is True:
        losses.note("async_tool_calling", f"tool {name} declared async")
    if tool.get("type") == "custom" and tool.get("output_schema") is not None:
        losses.note("tool_output_schema", f"freeform tool {name} declares an output schema")
    callers = tool.get("allowed_callers")
    if not isinstance(callers, list) or callers == ["direct"]:
        return True
    listed = ", ".join(str(caller) for caller in cast("list[Any]", callers))
    if "direct" not in callers:
        losses.note("programmatic_tool_calling", f"tool {name} callable only by {listed}: left out")
        return False
    losses.note("programmatic_tool_calling", f"tool {name} callable by {listed}")
    return True


def _convert_tool_choice(
    choice: Any, tools: list[ToolParam], losses: LossLog
) -> tuple[ToolChoice | None, list[ToolParam]]:
    """The tool choice, and the tools the model may call under it."""
    if choice == "auto":
        return ToolChoiceAuto(), tools
    if choice == "none":
        return ToolChoiceNone(), tools
    if choice == "required":
        return ToolChoiceRequired(), tools
    data = _as_dict(choice)
    if data is None:
        return None, tools
    kind = data.get("type")
    if kind in ("function", "custom"):
        # A forced choice names the tool only; whether it is a freeform tool is a matter of its
        # declaration, which the encoders consult when rendering the choice back.
        name = data.get("name")
        return ToolChoiceTool(name=name if isinstance(name, str) else ""), tools
    if kind == "allowed_tools":
        # A subset of the declared tools the model may call: the vocabulary's choices range
        # over all of the request's tools, so the others are left out of the request (the API
        # keeps them in the prompt, for its cache, but uncallable).
        allowed = _allowed_tools(data.get("tools"))
        kept = [tool for tool in tools if _is_allowed(tool, allowed)]
        if len(kept) < len(tools):
            losses.note(
                "allowed_tools",
                f"{len(tools) - len(kept)} declared tool(s) outside allowed_tools left out",
            )
        mode = data.get("mode")
        return (ToolChoiceRequired() if mode == "required" else ToolChoiceAuto()), kept
    # A tool on a hosted MCP server, or a hosted tool: the vocabulary's choices name only the
    # request's own tools.
    losses.note("tool_choice", f"tool_choice {kind}")
    return None, tools


def _allowed_tools(entries: Any) -> list[tuple[str, str | None, bool]]:
    """What an `allowed_tools` list names, as (name, namespace, whole namespace): a function or
    custom tool by name (in the namespace given, or in any), or a whole `namespace` entry."""
    out: list[tuple[str, str | None, bool]] = []
    for entry in _dict_list(entries):
        name = entry.get("name")
        if not isinstance(name, str):
            continue  # a hosted tool or MCP server, which the vocabulary has left out already
        namespace = entry.get("namespace")
        whole = entry.get("type") == "namespace"
        out.append((name, namespace if isinstance(namespace, str) else None, whole))
    return out


def _is_allowed(tool: ToolParam, allowed: list[tuple[str, str | None, bool]]) -> bool:
    for name, namespace, whole in allowed:
        if whole:
            if tool.namespace == name:
                return True
        elif tool.name == name and namespace in (None, tool.namespace):
            return True
    return False


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


# --- response: subject-model stream -> Responses --------------------------------------------------

# FailureCategory -> the dialect's (HTTP status, error `type`, error `code`). The codes are the
# OpenAI codes the Codex client special-cases (`is_context_window_error` etc. in its SSE consumer).
_FAILURE_STATUS_TYPES: dict[FailureCategory, tuple[int, str, str]] = {
    "context_length": (400, "invalid_request_error", "context_length_exceeded"),
    "content_filter": (400, "invalid_request_error", "content_policy_violation"),
    "rate_limited": (429, "rate_limit_error", "rate_limit_exceeded"),
    "overloaded": (503, "server_error", "server_is_overloaded"),
    "authentication": (401, "invalid_request_error", "invalid_api_key"),
    "invalid_request": (400, "invalid_request_error", "invalid_request"),
    "server_error": (500, "server_error", "server_error"),
    "adaptation_refused": (400, "invalid_request_error", "invalid_request"),
}

# The internal error vocabulary (errors a server synthesizes itself: unknown endpoint, bad JSON,
# ...) mapped to OpenAI error codes. Unmapped types pass through verbatim.
_ERROR_CODES = {
    "rate_limit_error": "rate_limit_exceeded",
    "overloaded_error": "server_is_overloaded",
    "authentication_error": "invalid_api_key",
    "invalid_request_error": "invalid_request",
    "not_found_error": "not_found",
    "api_error": "server_error",
}


def _error_code(anthropic_type: str) -> str:
    return _ERROR_CODES.get(anthropic_type, anthropic_type)


def sse_event(data: dict[str, object]) -> bytes:
    """Encode one Responses SSE event; the `event:` line carries the payload's `type`."""
    payload = json.dumps(data, separators=(",", ":"))
    return f"event: {data['type']}\ndata: {payload}\n\n".encode()


def _new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


def _new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def _new_reasoning_id() -> str:
    return f"{SYNTHETIC_REASONING_ID_PREFIX}{uuid.uuid4().hex}"


def _new_custom_tool_call_id() -> str:
    return f"ctc_{uuid.uuid4().hex}"


def _harness_reasoning_item(
    text: str, form: ReasoningForm, state: DynamicData | None
) -> dict[str, object]:
    """A reasoning block as a completed `reasoning` output item for the harness to echo.

    OpenAI's own state goes out as it was issued: the item's id (a synthetic one when the item
    had none) and its `encrypted_content`, with full reasoning text in `content` as the provider
    sent it. Anything else, another provider's state or none at all, rides `encrypted_content`
    in an envelope under a synthetic id, with its text in `summary`: Codex re-sends `summary` and
    `encrypted_content` on continuations but strips `reasoning_text` content
    (`should_serialize_reasoning_content` in its protocol crate).
    """
    native = own_reasoning_state(state, OpenAIResponsesReasoning)
    if native is not None:
        item_id = native.id or _new_reasoning_id()
        encrypted = native.encrypted_content
        in_content = form == "full"
    else:
        item_id = _new_reasoning_id()
        encrypted = envelope_text(form, state)
        in_content = False
    item: dict[str, object] = {
        "id": item_id,
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text}] if text and not in_content else [],
    }
    if text and in_content:
        item["content"] = [{"type": "reasoning_text", "text": text}]
    item["encrypted_content"] = encrypted
    return item


def _renders_to_harness(block: ContentReasoning) -> bool:
    """Whether a reasoning block has anything for a harness to echo: text, state, or the fact of
    redaction."""
    return bool(block.text) or block.reasoning_state is not None or block.form == "redacted"


def _custom_input(arguments: str) -> str:
    """The freeform text a call to a custom tool carries: the `input` field of the one-field
    object the vocabulary prescribes, or the raw arguments when they are not that object."""
    try:
        parsed: Any = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return arguments
    if isinstance(parsed, dict):
        value = cast("dict[str, Any]", parsed).get("input")
        if isinstance(value, str):
            return value
    return arguments


def _namespace_field(namespace: str | None) -> dict[str, object]:
    return {"namespace": namespace} if namespace is not None else {}


# The group the API treats as its own implicit one: a call to a tool declared under it comes
# back from the API without a namespace (observed with Codex's code-mode `exec`, declared under
# `functions`, 2026-10-07), so a synthesized call leaves it unnamed as well.
_IMPLICIT_NAMESPACE = "functions"


def _synthesized_namespace(resolved: ResolvedCall) -> str | None:
    """The namespace a call synthesized for the harness names: the one the subject's call
    carried or spelled as a flattened alias, else the declaration's (a subject without
    namespaces called the tool by its own name, and the harness routes by group), except the
    API's implicit group, which the API itself leaves unnamed."""
    if resolved.how in ("namespaced", "alias"):
        return resolved.namespace
    if resolved.how == "own" and resolved.namespace != _IMPLICIT_NAMESPACE:
        return resolved.namespace
    return None


def _historical_namespace(resolved: ResolvedCall) -> str | None:
    """The namespace a call in history is rendered with: as it was made, which is the group it
    carried or the one a flattened alias stood for; a call made by the unqualified own name
    stays unqualified (the API's own items are spelled that way for its implicit group)."""
    return resolved.namespace if resolved.how in ("namespaced", "alias") else None


def _new_function_call_id() -> str:
    return f"fc_{uuid.uuid4().hex}"


def _usage_object(usage: Usage) -> dict[str, object]:
    """The Responses `usage` object.

    OpenAI's `input_tokens` is the total *including* the tokens read from and written to the
    prompt cache, while `Usage.input_tokens` is what was neither — hence the sum. The Codex
    client requires the totals to be integers. `cache_write_tokens` is rendered only when the
    subject reported one, so a response without it reconstructs without it.
    """
    input_tokens = (
        usage.input_tokens
        + (usage.cache_read_input_tokens or 0)
        + (usage.cache_write_input_tokens or 0)
    )
    input_details: dict[str, object] = {"cached_tokens": usage.cache_read_input_tokens or 0}
    if usage.cache_write_input_tokens is not None:
        input_details["cache_write_tokens"] = usage.cache_write_input_tokens
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": input_details,
        "output_tokens": usage.output_tokens,
        "output_tokens_details": {"reasoning_tokens": usage.reasoning_output_tokens or 0},
        "total_tokens": input_tokens + usage.output_tokens,
    }


class _StreamState:
    """Tracks the open Responses output item while replaying subject-model deltas.

    The Responses stream opens one output item at a time (`response.output_item.added` … `.done`);
    the subject-model stream interleaves a text delta stream with tool-call deltas keyed by an
    integer tool ordinal. This maps tool ordinals to output items and closes the open item before
    the next. It also accumulates the complete items, because `response.output_item.done` must
    carry the full item (that event, not the deltas, is what the Codex client treats as
    authoritative) and `response.completed` echoes the whole `output` array.

    A reasoning block is held and rendered as one complete `reasoning` item when it ends (at the
    next event of any other kind, or a new block): `response.output_item.added` names the item's
    id, which for OpenAI's own state is the provider's and arrives with the state, after the text.
    A text delta with no block open (a producer that opens none) starts a block of full
    reasoning.
    """

    def __init__(self, sequencer: _Sequencer, tools: ToolNames | None = None) -> None:
        self._seq = sequencer
        self._tools = tools if tools is not None else ToolNames()
        self._open: _OpenItem | None = None
        self._tool_item: dict[int, str] = {}  # tool ordinal -> item id (dedup only)
        self._held: _HeldReasoning | None = None
        self.output: list[dict[str, object]] = []

    def process(self, event: StreamEvent) -> list[bytes]:
        if isinstance(event, StreamReasoningStart):
            out = self._flush_reasoning()
            self._held = _HeldReasoning(event.form)
            return out
        if isinstance(event, StreamReasoningDelta):
            if not event.text:
                return []
            # Text after the block's state belongs to a new block.
            out = self._hold("full")
            assert self._held is not None
            self._held.parts.append(event.text)
            return out
        if isinstance(event, StreamReasoningState):
            out = self._hold("summary")
            assert self._held is not None
            self._held.state = event.state
            return out
        out: list[bytes] = self._flush_reasoning()
        if isinstance(event, StreamTextDelta) and event.text:
            item = self._open
            if item is None or item.kind != "text":
                out += self._close_open()
                item = self._open_text(out)
            item.text += event.text
            out.append(
                self._seq.event(
                    {
                        "type": "response.output_text.delta",
                        "item_id": item.item_id,
                        "output_index": item.output_index,
                        "content_index": 0,
                        "delta": event.text,
                        "logprobs": [],
                    }
                )
            )
        elif isinstance(event, StreamToolUseStart):
            out += self._close_open()
            self._tool_item[event.index] = self._open_tool_call(
                out, event.id, event.name, event.namespace
            )
        elif isinstance(event, StreamToolUseInputDelta) and event.partial_json:
            item = self._open
            if item is None or item.item_id != self._tool_item.get(event.index):
                # Out-of-order interleaving (an already-closed tool call receives more deltas)
                # cannot be represented; the closed item already carried what it had.
                return out
            item.arguments += event.partial_json
            if item.kind == "custom_tool_call":
                # The freeform text is a field of a JSON object still streaming in; it is
                # emitted whole when the item closes.
                return out
            out.append(
                self._seq.event(
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item.item_id,
                        "output_index": item.output_index,
                        "delta": event.partial_json,
                    }
                )
            )
        return out

    def _open_text(self, out: list[bytes]) -> _OpenItem:
        """Open a message item whose one content part is text."""
        # Only called with no item open (after _close_open), so the next index is len(output).
        item = _OpenItem(
            kind="text",
            item_id=_new_message_id(),
            output_index=len(self.output),
        )
        self._open = item
        out.append(
            self._seq.event(
                {
                    "type": "response.output_item.added",
                    "output_index": item.output_index,
                    "item": {
                        "id": item.item_id,
                        "type": "message",
                        "status": "in_progress",
                        "content": [],
                        "role": "assistant",
                    },
                }
            )
        )
        out.append(
            self._seq.event(
                {
                    "type": "response.content_part.added",
                    "item_id": item.item_id,
                    "output_index": item.output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": ""},
                }
            )
        )
        return item

    def _hold(self, form: ReasoningForm) -> list[bytes]:
        """Make sure a block that can still take text and state is held: the held one, unless it
        already has its state (then it is rendered first), or a new one of `form`."""
        if self._held is not None and self._held.state is None:
            return []
        out = self._flush_reasoning()
        self._held = _HeldReasoning(form)
        return out

    def _open_tool_call(
        self, out: list[bytes], call_id: str, name: str, namespace: str | None
    ) -> str:
        # The subject's call, resolved against the harness's declarations: a freeform tool is
        # rendered as a `custom_tool_call`, a grouped one carries its namespace, and a name a
        # namespace-less dialect used maps back to the declaration it stood for.
        resolved = self._tools.resolve_call(name, namespace)
        tool = resolved.tool
        custom = tool is not None and tool.freeform_format is not None
        item = _OpenItem(
            kind="custom_tool_call" if custom else "function_call",
            item_id=_new_custom_tool_call_id() if custom else _new_function_call_id(),
            output_index=len(self.output),
        )
        item.call_id = call_id
        item.name = resolved.name
        item.namespace = _synthesized_namespace(resolved)
        self._open = item
        out.append(
            self._seq.event(
                {
                    "type": "response.output_item.added",
                    "output_index": item.output_index,
                    "item": {
                        "id": item.item_id,
                        "type": item.kind,
                        "status": "in_progress",
                        **({"input": ""} if custom else {"arguments": ""}),
                        "call_id": item.call_id,
                        "name": item.name,
                        **_namespace_field(item.namespace),
                    },
                }
            )
        )
        return item.item_id

    def _close_open(self) -> list[bytes]:
        item, self._open = self._open, None
        if item is None:
            return []
        out: list[bytes] = []
        if item.kind == "text":
            done_part: dict[str, object] = {
                "type": "output_text",
                "annotations": [],
                "logprobs": [],
                "text": item.text,
            }
            out.append(
                self._seq.event(
                    {
                        "type": "response.output_text.done",
                        "item_id": item.item_id,
                        "output_index": item.output_index,
                        "content_index": 0,
                        "logprobs": [],
                        "text": item.text,
                    }
                )
            )
            out.append(
                self._seq.event(
                    {
                        "type": "response.content_part.done",
                        "item_id": item.item_id,
                        "output_index": item.output_index,
                        "content_index": 0,
                        "part": done_part,
                    }
                )
            )
            completed = {
                "id": item.item_id,
                "type": "message",
                "status": "completed",
                "content": [done_part],
                "role": "assistant",
            }
        elif item.kind == "function_call":
            out.append(
                self._seq.event(
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item.item_id,
                        "output_index": item.output_index,
                        "arguments": item.arguments,
                    }
                )
            )
            completed = {
                "id": item.item_id,
                "type": "function_call",
                "status": "completed",
                "arguments": item.arguments,
                "call_id": item.call_id,
                "name": item.name,
                **_namespace_field(item.namespace),
            }
        else:
            text = _custom_input(item.arguments)
            for suffix, key in (("delta", "delta"), ("done", "input")):
                out.append(
                    self._seq.event(
                        {
                            "type": f"response.custom_tool_call_input.{suffix}",
                            "item_id": item.item_id,
                            "output_index": item.output_index,
                            key: text,
                        }
                    )
                )
            completed = {
                "id": item.item_id,
                "type": "custom_tool_call",
                "status": "completed",
                "input": text,
                "call_id": item.call_id,
                "name": item.name,
                **_namespace_field(item.namespace),
            }
        self.output.append(completed)
        out.append(
            self._seq.event(
                {
                    "type": "response.output_item.done",
                    "output_index": item.output_index,
                    "item": completed,
                }
            )
        )
        return out

    def _flush_reasoning(self) -> list[bytes]:
        held, self._held = self._held, None
        if held is None:
            return []
        block = ContentReasoning(
            text="".join(held.parts), form=held.form, reasoning_state=held.state
        )
        if not _renders_to_harness(block):
            return []
        out = self._close_open()
        output_index = len(self.output)
        completed = _harness_reasoning_item(block.text, block.form, block.reasoning_state)
        item_id = completed["id"]
        out.append(
            self._seq.event(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": {"id": item_id, "type": "reasoning", "summary": []},
                }
            )
        )
        if block.text and "content" in completed:
            common = {"item_id": item_id, "output_index": output_index, "content_index": 0}
            out.append(
                self._seq.event(
                    {"type": "response.reasoning_text.delta", **common, "delta": block.text}
                )
            )
            out.append(
                self._seq.event(
                    {"type": "response.reasoning_text.done", **common, "text": block.text}
                )
            )
        elif block.text:
            common = {"item_id": item_id, "output_index": output_index, "summary_index": 0}
            out.append(
                self._seq.event(
                    {
                        "type": "response.reasoning_summary_part.added",
                        **common,
                        "part": {"type": "summary_text", "text": ""},
                    }
                )
            )
            out.append(
                self._seq.event(
                    {"type": "response.reasoning_summary_text.delta", **common, "delta": block.text}
                )
            )
            out.append(
                self._seq.event(
                    {"type": "response.reasoning_summary_text.done", **common, "text": block.text}
                )
            )
            out.append(
                self._seq.event(
                    {
                        "type": "response.reasoning_summary_part.done",
                        **common,
                        "part": {"type": "summary_text", "text": block.text},
                    }
                )
            )
        self.output.append(completed)
        out.append(
            self._seq.event(
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": completed,
                }
            )
        )
        return out

    def close(self) -> list[bytes]:
        return self._flush_reasoning() + self._close_open()


class _HeldReasoning:
    """A reasoning block being held until it ends (see `_StreamState`)."""

    def __init__(self, form: ReasoningForm) -> None:
        self.form: ReasoningForm = form
        self.parts: list[str] = []
        self.state: DynamicData | None = None


class _OpenItem:
    """Mutable accumulator for the output item currently being streamed."""

    def __init__(self, kind: str, item_id: str, output_index: int) -> None:
        self.kind = kind
        self.item_id = item_id
        self.output_index = output_index
        self.text = ""
        self.arguments = ""
        self.call_id = ""
        self.name = ""
        self.namespace: str | None = None


class _Sequencer:
    """Stamps the monotonic `sequence_number` the Responses stream carries on every event."""

    def __init__(self) -> None:
        self._next = 0

    def event(self, data: dict[str, object]) -> bytes:
        data["sequence_number"] = self._next
        self._next += 1
        return sse_event(data)


def _response_object(
    response_id: str,
    model: str,
    status: str,
    output: list[dict[str, object]],
    usage: Usage | None,
    created_at: int,
) -> dict[str, object]:
    """A synthesized Responses `Response` object.

    Carries the full standard field set (the OpenAI SDK pydantic-validates `response` payloads;
    the Codex client only reads a subset). `tool_choice`/`tools` are fixed defaults rather than
    echoes of the request — the synthesizer sees only the subject-model stream.
    """
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "background": False,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": None,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": None,
        "service_tier": (
            None if usage is None or usage.speed is None else service_tier_param(usage.speed)
        ),
        "store": False,
        "temperature": None,
        "text": None,
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": _usage_object(usage) if usage is not None else None,
        "user": None,
        "metadata": {},
    }


async def synthesize_responses_sse(
    model: str, items: AsyncIterator[SubjectStreamItem], *, tools: tuple[ToolParam, ...] = ()
) -> AsyncIterator[bytes]:
    """Replay a subject-model stream as the Responses streaming SSE event sequence.

    A terminal `ModelCallFailure` (reached after deltas already streamed) is surfaced as a
    `response.failed` event — matching how the real API reports an in-flight failure — rather
    than an HTTP status (the status is already committed). Failures with no prior deltas are
    handled by the backend before this is called. A `StopMaxTokens` stop reason ends the stream
    with `response.incomplete` instead of `response.completed`, as the real API does.
    """
    seq = _Sequencer()
    response_id = _new_response_id()
    created_at = int(time.time())
    yield seq.event(
        {
            "type": "response.created",
            "response": _response_object(response_id, model, "in_progress", [], None, created_at),
        }
    )
    yield seq.event(
        {
            "type": "response.in_progress",
            "response": _response_object(response_id, model, "in_progress", [], None, created_at),
        }
    )
    state = _StreamState(seq, ToolNames(tools))
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
        _, _, code = _FAILURE_STATUS_TYPES[failure.category]
        failed = _response_object(response_id, model, "failed", state.output, None, created_at)
        failed["error"] = {"code": code, "message": failure_message(failure)}
        yield seq.event({"type": "response.failed", "response": failed})
        return
    reason = _render_stop(result.stop_reason, streaming=True).incomplete_reason
    if reason is not None:
        incomplete = _response_object(
            response_id, model, "incomplete", state.output, result.usage, created_at
        )
        incomplete["incomplete_details"] = {"reason": reason}
        yield seq.event({"type": "response.incomplete", "response": incomplete})
        return
    # A refusal streamed as `output_text` stays that part (its type was fixed at the first
    # delta, before the stop reason existed): a recorded response loss, `refusal_as_text`.
    yield seq.event(
        {
            "type": "response.completed",
            "response": _response_object(
                response_id, model, "completed", state.output, result.usage, created_at
            ),
        }
    )


def _output_items(
    output: tuple[ChatMessage, ...], tools: ToolNames | None = None, *, refusal: bool = False
) -> list[dict[str, object]]:
    """An assembled response's content as completed Responses output items.

    Consecutive text blocks merge into one message item, its part a `refusal` when `refusal`
    (the response is the model's whole refusal, `StopRefusal(complete=True)`); each tool-use
    block becomes its own
    `function_call` item (arguments re-encoded from the parsed input), or a `custom_tool_call`
    item carrying the freeform text when the request declared the tool freeform; each reasoning
    block becomes its own `reasoning` item (see `_harness_reasoning_item`).
    """
    names = tools if tools is not None else ToolNames()
    items: list[dict[str, object]] = []
    text_parts: list[str] = []

    def flush_text() -> None:
        if text_parts:
            text = "".join(text_parts)
            part: dict[str, object] = (
                {"type": "refusal", "refusal": text}
                if refusal
                else {"type": "output_text", "annotations": [], "logprobs": [], "text": text}
            )
            items.append(
                {
                    "id": _new_message_id(),
                    "type": "message",
                    "status": "completed",
                    "content": [part],
                    "role": "assistant",
                }
            )
            text_parts.clear()

    for message in output:
        if not isinstance(message, AssistantMessage):
            continue  # server-side tool exchanges have no representation here
        for block in message.content:
            block_item = _output_item_for_block(block, names)
            if isinstance(block, ContentText):
                if block.text:
                    text_parts.append(block.text)
            elif block_item is not None:
                flush_text()
                items.append(block_item)
    flush_text()
    return items


def _output_item_for_block(block: Content, names: ToolNames) -> dict[str, object] | None:
    if isinstance(block, ContentToolUse):
        resolved = names.resolve_call(block.name, block.namespace)
        tool = resolved.tool
        name = resolved.name
        namespace = _synthesized_namespace(resolved)
        if tool is not None and tool.freeform_format is not None:
            return {
                "id": _new_custom_tool_call_id(),
                "type": "custom_tool_call",
                "status": "completed",
                "input": _custom_input(json.dumps(block.input)),
                "call_id": block.id,
                "name": name,
                **_namespace_field(namespace),
            }
        return {
            "id": _new_function_call_id(),
            "type": "function_call",
            "status": "completed",
            "arguments": json.dumps(block.input),
            "call_id": block.id,
            "name": name,
            **_namespace_field(namespace),
        }
    if isinstance(block, ContentReasoning) and _renders_to_harness(block):
        return _harness_reasoning_item(block.text, block.form, block.reasoning_state)
    return None


def response_to_responses_response(
    model: str, response: ModelResponse, *, tools: tuple[ToolParam, ...] = ()
) -> dict[str, object]:
    """Render an assembled response as a non-streaming Responses `Response` body.

    Codex always streams, so this covers only a request with `stream` false or absent. The
    response id is not echoed (it may be another provider's shape); a fresh `resp_` id is
    synthesized, and the real id stays on the `ModelResponse`. `tools` are the request's
    declarations, which decide how each call is rendered (see `_output_items`).
    """
    rendered = _render_stop(response.stop_reason, streaming=False)
    reason = rendered.incomplete_reason
    body = _response_object(
        _new_response_id(),
        model,
        "completed" if reason is None else "incomplete",
        _output_items(
            response.output,
            ToolNames(tools),
            refusal=rendered.refusal_part,
        ),
        response.usage,
        int(time.time()),
    )
    if reason is not None:
        body["incomplete_details"] = {"reason": reason}
    return body


# A `StopOther` decoded from a Responses response incomplete for a reason other than the two
# with variants of their own carries that reason after this prefix (`_model_stop_reason`).
_INCOMPLETE_PREFIX = "incomplete: "


@dataclass(frozen=True)
class _RenderedStop:
    incomplete_reason: str | None = None
    refusal_part: bool = False
    rendering: StopRendering = StopRendering()


def _render_stop(stop: StopReason, *, streaming: bool) -> _RenderedStop:
    """An assembled stop reason as a Responses ending, with what that says other than as given.
    One mapping serves the encoders and `render_stop`.

    `incomplete_reason` is the `incomplete_details.reason` of an incomplete response, None for a
    completed one; `refusal_part` renders the text as a `refusal` part. A refusal that is the
    model's whole answer is a completed response with a refusal part, except streamed: a part's
    type is fixed at its first delta, before the stop reason exists, so it stays `output_text`.
    A refusal cut off or blocked, or one the provider did not say which of, and a content
    filter, are incomplete for `content_filter`.
    """
    if isinstance(stop, StopMaxTokens):
        return _RenderedStop("max_output_tokens")
    if isinstance(stop, StopRefusal):
        if stop.complete is None:
            loss = ResponseLoss(
                "refusal_completeness",
                f"a refusal ({stop.provider_reason}) the provider did not say was complete, "
                "sent as incomplete for content_filter",
            )
            return _RenderedStop("content_filter", rendering=StopRendering(losses=(loss,)))
        if not stop.complete:
            return _RenderedStop("content_filter")
        if streaming:
            loss = ResponseLoss(
                "refusal_as_text",
                "the model's refusal streamed as output_text, not a refusal part (the part's "
                "type is fixed at its first delta, before the stop reason is known)",
            )
            return _RenderedStop(rendering=StopRendering(losses=(loss,)))
        return _RenderedStop(refusal_part=True)
    if isinstance(stop, StopContentFilter):
        return _RenderedStop("content_filter")
    if isinstance(stop, StopOther):
        if stop.provider_reason.startswith(_INCOMPLETE_PREFIX):
            return _RenderedStop(stop.provider_reason.removeprefix(_INCOMPLETE_PREFIX))
        # The API's incomplete reasons are a token limit and a content filter, and completed
        # would read as a finished answer: any other stop (Gemini's MALFORMED_FUNCTION_CALL,
        # say) is answered as an error.
        loss = ResponseLoss(
            "stop_reason",
            f"a stop with no Responses form ({stop.provider_reason}) answered as an error",
        )
        return _RenderedStop(rendering=StopRendering(losses=(loss,), as_error=True))
    return _RenderedStop()


def responses_failure_body(failure: ModelCallFailure) -> tuple[int, bytes]:
    """A request-time failure as the dialect's (HTTP status, error body)."""
    status, error_type, code = _FAILURE_STATUS_TYPES[failure.category]
    payload = json.dumps(
        {
            "error": {
                "message": failure_message(failure),
                "type": error_type,
                "param": None,
                "code": code,
            }
        }
    ).encode("utf-8")
    return status, payload


def responses_error_body(anthropic_type: str, message: str) -> bytes:
    """Encode an OpenAI-shaped error body (`{"error": {...}}`) from the internal error type."""
    return json.dumps(
        {
            "error": {
                "message": message,
                "type": anthropic_type,
                "param": None,
                "code": _error_code(anthropic_type),
            }
        }
    ).encode("utf-8")


_ENDPOINT = "/v1/responses"


class OpenAIResponsesCodec:
    """The OpenAI Responses dialect (`/v1/responses`), both halves.

    Args:
        extra_body: Provider parameters filled in where a request leaves the key absent.
    """

    dialect: Dialect = "openai_responses"
    reasoning_round_trip = True

    def __init__(self, *, extra_body: dict[str, Any] | None = None) -> None:
        self._extra_body = dict(extra_body) if extra_body else {}

    # --- model side -----------------------------------------------------------------------------

    def adapt_request(self, model: str, request: ModelRequest) -> AdaptedRequest:
        return adapt_responses_request(
            model,
            request,
            lambda state: self.accepts_reasoning_state(state, model),
            self._extra_body,
        )

    def encode_request(
        self, model: str, request: ModelRequest, *, streaming: bool
    ) -> EncodedRequest:
        return encode_responses_request(
            model, request, streaming=streaming, extra_body=self._extra_body
        )

    def sse_decoder(self) -> SseDecoder:
        return ResponsesSseDecoder()

    def decode_response(self, body: bytes) -> ModelResponse | ModelCallFailure:
        return decode_responses_response(body)

    def decode_failure(self, status: int, body: bytes) -> ModelCallFailure:
        return decode_responses_failure(status, body)

    def accepts_reasoning_state(self, state: DynamicData, model: str) -> bool:
        """OpenAI's own reasoning items only: the API verifies `encrypted_content` it issued and
        rejects anything else with a 400 (`invalid_encrypted_content`). The payload is not bound
        to a model."""
        return own_reasoning_state(state, OpenAIResponsesReasoning) is not None

    # --- harness side ---------------------------------------------------------------------------

    def matches_path(self, target: str) -> bool:
        return path_matches(target, _ENDPOINT)

    def matches_headers(self, headers: Headers) -> bool:
        return False  # the Responses API has no header that identifies it

    def matches_body(self, body: JSONObject) -> bool:
        return "input" in body and "messages" not in body

    def route(self, target: str, body: JSONObject) -> Route:
        path = normalized_path(target)
        model = body.get("model")
        kind: Literal["completion", "other"] = (
            "completion" if path == _ENDPOINT or path.endswith(_ENDPOINT) else "other"
        )
        return Route(
            model=model if isinstance(model, str) else None,
            streaming=bool(body.get("stream", False)),
            kind=kind,
        )

    def decode_request(self, target: str, headers: Headers, body: bytes) -> DecodedRequest:
        return responses_request_to_canonical(_json_object(body), headers, target)

    def provider_tools(self, body: JSONObject) -> tuple[ProviderTool, ...]:
        return provider_tools_in_request(cast("dict[str, Any]", body))

    def strip_reasoning(self, request: ModelRequest) -> ModelRequest:
        return without_reasoning(request, (("reasoning",),))

    def encode_sse(
        self,
        model: str,
        items: AsyncIterator[SubjectStreamItem],
        *,
        tools: tuple[ToolParam, ...] = (),
    ) -> AsyncIterator[bytes]:
        return synthesize_responses_sse(model, items, tools=tools)

    def keepalive_frame(self) -> bytes | None:
        # The Responses API defines no keepalive event. An event with a `data:` line is what a
        # client's idle timeout counts (an SSE comment is dropped before it: Codex's reader times
        # out on `eventsource` events), and clients skip a `type` they do not know (Codex logs
        # it at trace level).
        return sse_event({"type": "keepalive"})

    def encode_response(
        self, model: str, response: ModelResponse, *, tools: tuple[ToolParam, ...] = ()
    ) -> JSONObject:
        return cast("JSONObject", response_to_responses_response(model, response, tools=tools))

    def encode_failure(self, failure: ModelCallFailure) -> tuple[int, bytes]:
        return responses_failure_body(failure)

    def render_stop(self, stop: StopReason, *, streaming: bool) -> StopRendering:
        return _render_stop(stop, streaming=streaming).rendering

    def encode_error(self, status: int, error_type: str, message: str) -> bytes:
        return responses_error_body(error_type, message)


def _json_object(body: bytes) -> dict[str, Any]:
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("request body is not a JSON object")
    return cast("dict[str, Any]", parsed)


# --- model side: ModelRequest -> Responses request body -------------------------------------------
#
# Rendering notes:
#
# - A `SystemMessage` that opens the context renders as the top-level `instructions`; every
#   other one renders as a `developer` message item at its position, which is how Codex sends
#   its permissions and skills preamble (after `instructions`) and how a mid-conversation system
#   message from another dialect keeps its place. A context that opens with a system message
#   decoded from a `system`/`developer` input item (no `instructions` on the wire) therefore
#   re-encodes as `instructions`: the vocabulary does not record which slot it came from.
# - Reasoning with OpenAI's own state (`OpenAIResponsesReasoning`, the only state
#   `accepts_reasoning_state` lets through) renders as a `reasoning` input item with its id and
#   `encrypted_content`, which the API verifies against each other. A state whose item had no id
#   renders without one, which the API accepts (it reads the identity from the payload); an
#   invented id is rejected.
# - Full reasoning without state renders as a `reasoning` item carrying the text in `content`,
#   the shape OpenAI-compatible Responses servers for open-weight models emit and accept back.
#   A summary or a redaction without state has no representation; adaptation removes it.
# - `SamplingParams.reasoning` maps to `reasoning.effort` (see `_openai_common.reasoning_effort`).
#   `reasoning.summary: "auto"` is a declared default: filled in whenever reasoning is on and the
#   request did not choose a summary mode, so summaries stream back.
# - `store: false` is a declared default: the API is stateful by default, and the full context is
#   always sent, so nothing is stored unless a request asks to.
# - `stop_sequences`, `ToolMessage.is_error` and a reasoning budget (as opposed to an effort) have
#   no Responses API form; adaptation removes the first two and sends a budget as its nearest
#   effort.
# - Cache breakpoints render as `prompt_cache_breakpoint` on an `input_text` part, at the end of
#   the marked block: on a message's part (an assistant's text part becomes `input_text` to take
#   one), and on a tool output's part (the output becomes a list; a breakpoint on the whole
#   result goes on its last part). A marked leading system message renders as a developer item
#   rather than `instructions`, which take none. Breakpoints on tool definitions and tool calls
#   have no Responses form; adaptation removes them. Models before gpt-5.6 reject breakpoints and
#   `prompt_cache_options` alike; `OpenAIResponsesAPI` then resends without both, which is the
#   implicit caching mode.
# - A cache TTL is the provider's retention setting, not part of the prompt. A breakpoint's
#   `30m` or `12h` TTL overrides `prompt_cache_options.ttl`; an omitted TTL inherits that request
#   setting, wherever the request sets it (`_request_cache_ttl`). A TTL is a minimum: adaptation
#   gives a tool result and its marked final part, which share a boundary, the longer of their
#   TTLs, then fits one the API does not offer (an Anthropic `5m` or `1h`) to the shortest it
#   offers that is at least as long, else the longest; each change is a delivery loss. A
#   same-dialect request keeps its `prompt_cache_options` through the extras.
# - Without `prompt_cache_options` (a translated request) the API keeps its implicit breakpoint
#   and writes the latest three explicit ones; every breakpoint is still rendered, since the API
#   also matches cached prefixes at breakpoints it does not write (many more, earlier in the
#   conversation).
# - `SamplingParams.extra` keys are forwarded verbatim in the request body for the provider to
#   validate; the codec's `extra_body` fills in only where the request left a key absent.


def _text_of(content: tuple[ContentText, ...]) -> str:
    return "".join(block.text for block in content)


# The lifetimes the API offers, shortest first, and the one a request without
# `prompt_cache_options.ttl` gets.
_CACHE_TTLS = ("30m", "12h")
_DEFAULT_CACHE_TTL = "30m"


def _input_text(block: ContentText, *, cache_control: CacheControl | None = None) -> dict[str, Any]:
    """An `input_text` part with its breakpoint and optional lifetime. A tool result's own
    breakpoint, passed explicitly, takes precedence on the result's final part."""
    part: dict[str, Any] = {"type": "input_text", "text": block.text}
    cache_control = cache_control or _breakpoint(block)
    if cache_control is not None:
        breakpoint: dict[str, Any] = {"mode": "explicit"}
        if cache_control.ttl is not None:
            if cache_control.ttl not in _CACHE_TTLS:
                raise unadapted("an unsupported cache breakpoint TTL")
            breakpoint["ttl"] = cache_control.ttl
        part["prompt_cache_breakpoint"] = breakpoint
    return part


def _synthetic_reasoning_ids() -> Iterator[str]:
    """Ids for reasoning items that carry none of their own, unique within one request."""
    return (f"{SYNTHETIC_REASONING_ID_PREFIX}{n}" for n in itertools.count())


def _assistant_items(
    message: AssistantMessage, synthetic_ids: Iterator[str], names: ToolNames
) -> list[dict[str, Any]]:
    """One input item per content block, preserving block order, except that consecutive text
    blocks are the parts of one assistant message; a call to a tool the request declared freeform
    is a `custom_tool_call` carrying its text, and a call to a grouped tool carries its
    namespace."""
    items: list[dict[str, Any]] = []
    text_parts: list[dict[str, Any]] | None = None  # the parts of the message being written
    for block in message.content:
        if not isinstance(block, ContentText):
            text_parts = None
        if isinstance(block, ContentText):
            if block.text:
                # Only an input part carries a breakpoint; the API takes one in an assistant
                # message too.
                part = (
                    _input_text(block)
                    if _breakpoint(block) is not None
                    else {"type": "output_text", "text": block.text}
                )
                if text_parts is None:
                    text_parts = []
                    items.append({"role": "assistant", "content": text_parts})
                text_parts.append(part)
        elif isinstance(block, ContentReasoning):
            reasoning = _reasoning_input_item(block, synthetic_ids)
            if reasoning is not None:
                items.append(reasoning)
        elif isinstance(block, ContentToolUse):
            resolved = names.resolve_call(block.name, block.namespace)
            tool = resolved.tool
            call: dict[str, Any] = {"call_id": block.id, "name": resolved.name}
            if tool is not None and tool.freeform_format is not None:
                call.update(type="custom_tool_call", input=_custom_input(json.dumps(block.input)))
            else:
                call.update(type="function_call", arguments=json.dumps(block.input))
            call.update(_namespace_field(_historical_namespace(resolved)))
            if isinstance(block, CacheableContentToolUse) and block.cache_control is not None:
                raise unadapted("a cache breakpoint on a tool call")
            items.append(call)
    return items


def _reasoning_input_item(
    block: ContentReasoning, synthetic_ids: Iterator[str]
) -> dict[str, Any] | None:
    """A reasoning block as an input item (see the rendering notes above), or None for one with
    neither text nor state."""
    native = own_reasoning_state(block.reasoning_state, OpenAIResponsesReasoning)
    if native is not None:
        item: dict[str, Any] = {"type": "reasoning"}
        if native.id is not None:
            item["id"] = native.id
        if block.form == "full" and block.text:
            item["summary"] = []
            item["content"] = [{"type": "reasoning_text", "text": block.text}]
        else:
            item["summary"] = [{"type": "summary_text", "text": block.text}] if block.text else []
        item["encrypted_content"] = native.encrypted_content
        return item
    if block.form == "full" and block.text:
        return {
            "type": "reasoning",
            "id": next(synthetic_ids),
            "summary": [],
            "content": [{"type": "reasoning_text", "text": block.text}],
        }
    if block.form == "full":
        return None  # no text and no state: nothing to send
    raise unadapted(f"a reasoning {block.form} without OpenAI state")


def _render_context(
    context: tuple[ChatMessage, ...], names: ToolNames | None = None
) -> tuple[str | None, list[dict[str, Any]]]:
    """Render messages into the top-level `instructions` and the `input` item list. `names`
    are the request's tool declarations, which decide how a historical call is rendered; the
    result of a call rendered as a `custom_tool_call` is a `custom_tool_call_output`."""
    instruction_parts: list[str] = []
    items: list[dict[str, Any]] = []
    synthetic_ids = _synthetic_reasoning_ids()
    names = names if names is not None else ToolNames()
    custom_call_ids: set[str] = set()
    for index, message in enumerate(context):
        if isinstance(message, SystemMessage):
            # `instructions` cannot carry a breakpoint, so a cached system prompt is the leading
            # developer item the API reads it as.
            if index == 0 and not any(_breakpoint(b) is not None for b in message.content):
                instruction_parts.append(_text_of(message.content))
            else:
                items.append(
                    {
                        "role": "developer",
                        "content": [_input_text(block) for block in message.content],
                    }
                )
        elif isinstance(message, UserMessage):
            items.append(
                {"role": "user", "content": [_input_text(block) for block in message.content]}
            )
        elif isinstance(message, ToolMessage):
            if message.is_error is not None:
                raise unadapted("a tool result's error flag")
            custom = message.tool_call_id in custom_call_ids
            output: dict[str, Any] = {
                "type": "custom_tool_call_output" if custom else "function_call_output"
            }
            # An output the harness sent without an id (the API allows it) goes back without
            # one rather than with an empty id.
            if message.tool_call_id:
                output["call_id"] = message.tool_call_id
            # A breakpoint on the result goes on its last text part, at its end; one on a text
            # block stays on that block's part.
            cache_control = (
                message.cache_control if isinstance(message, CacheableToolMessage) else None
            )
            if cache_control is not None or any(
                _breakpoint(block) is not None for block in message.content
            ):
                blocks = message.content or (ContentText(text=""),)
                parts = [_input_text(block) for block in blocks]
                if cache_control is not None:
                    parts[-1] = _input_text(blocks[-1], cache_control=cache_control)
                output["output"] = parts
            else:
                output["output"] = _text_of(message.content)
            items.append(output)
        else:
            assistant_items = _assistant_items(message, synthetic_ids, names)
            custom_call_ids.update(
                str(item["call_id"])
                for item in assistant_items
                if item.get("type") == "custom_tool_call"
            )
            items.extend(assistant_items)
    instructions = "\n\n".join(part for part in instruction_parts if part)
    return (instructions or None), items


def _tool_param(tool: ToolParam) -> dict[str, Any]:
    if isinstance(tool, CacheableToolParam) and tool.cache_control is not None:
        raise unadapted("a cache breakpoint on a tool definition")
    if tool.freeform_format is not None:
        param: dict[str, Any] = {
            "type": "custom",
            "name": tool.name,
            "format": copy.deepcopy(dict(tool.freeform_format)),
        }
    else:
        param = {
            "type": "function",
            "name": tool.name,
            "parameters": copy.deepcopy(dict(tool.input_schema)),
        }
        if tool.strict != "auto":  # omitted: strict when the schema can be made compatible
            param["strict"] = tool.strict
        if tool.output_schema is not None:
            param["output_schema"] = copy.deepcopy(dict(tool.output_schema))
    if tool.description is not None:
        param["description"] = tool.description
    return param


def _namespace_descriptions(tools: tuple[ToolParam, ...]) -> dict[str, str]:
    """Each group's description, which its members must agree on (`CodecError` otherwise:
    picking one would silently drop what another member says)."""
    described: dict[str, str] = {}
    for tool in tools:
        if tool.namespace is None or tool.namespace_description is None:
            continue
        known = described.setdefault(tool.namespace, tool.namespace_description)
        if known != tool.namespace_description:
            raise CodecError(
                f"namespace {tool.namespace!r} is described two ways by its members: "
                f"{known!r} and {tool.namespace_description!r}"
            )
    return described


def _tools_param(tools: tuple[ToolParam, ...]) -> list[dict[str, Any]]:
    """The `tools` array: ungrouped tools as they are, grouped ones under one `namespace` entry
    per group, in order of first appearance, with the group's description (the API requires
    one; a group no member describes gets an empty one)."""
    descriptions = _namespace_descriptions(tools)
    out: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    for tool in tools:
        if tool.namespace is None:
            out.append(_tool_param(tool))
            continue
        group = groups.get(tool.namespace)
        if group is None:
            group = groups[tool.namespace] = {
                "type": "namespace",
                "name": tool.namespace,
                "description": descriptions.get(tool.namespace, ""),
                "tools": [],
            }
            out.append(group)
        group["tools"].append(_tool_param(tool))
    return out


def _tool_choice_param(tool_choice: ToolChoice, names: ToolNames) -> Any:
    if isinstance(tool_choice, ToolChoiceAuto):
        return "auto"
    if isinstance(tool_choice, ToolChoiceRequired):
        return "required"
    if isinstance(tool_choice, ToolChoiceNone):
        return "none"
    # A choice names the tool only (the API has no namespace slot here); the declaration it
    # resolves to decides whether the choice is of a freeform tool.
    tool = names.resolve_choice(tool_choice.name)
    if tool is not None and tool.freeform_format is not None:
        return {"type": "custom", "name": tool.name}
    return {"type": "function", "name": tool_choice.name}


def _reasoning_param(reasoning: ReasoningParams | None) -> dict[str, str] | None:
    """The `reasoning` request object as the request asked for it, or None to omit it."""
    if reasoning is None:
        return None
    if reasoning.enabled and reasoning.effort is None and reasoning.budget_tokens is not None:
        raise unadapted("a reasoning budget")
    effort = reasoning_effort(reasoning)
    return {"effort": effort} if effort is not None else {}


def _apply_summary_choice(
    reasoning_body: dict[str, Any], reasoning: ReasoningParams | None
) -> None:
    """Settle `reasoning.summary` on a rendered request with reasoning on, reconciling the
    neutral choice with what a same-dialect extra retained (a key may be present with a null
    value, the API's explicit opt-out, which is not the same as absent):

    - `False` suppresses summaries: a retained mode goes, a retained null stays as spelled;
    - `True` keeps a retained mode (it already asks for summaries, with a detail level the
      vocabulary does not model) and otherwise renders the mode the vocabulary spells;
    - unspecified honors whatever the extra retained, null included, else the declared
      default `auto`.
    """
    choice = reasoning.summary if reasoning is not None else None
    present = "summary" in reasoning_body
    retained = reasoning_body.get("summary")
    if choice is False:
        if not (present and retained is None):
            reasoning_body.pop("summary", None)
    elif choice is True:
        if not (present and retained in _SUMMARY_MODES):
            reasoning_body["summary"] = _NEUTRAL_SUMMARY_MODE
    elif not present:
        reasoning_body["summary"] = "auto"


def adapt_responses_request(
    model: str,
    request: ModelRequest,
    accepts: ReasoningStateFilter,
    extra_body: dict[str, Any] | None = None,
) -> AdaptedRequest:
    """`request` changed so the Responses API can carry it, one loss per change.

    The API has no stop sequences and no error flag on a tool output. It takes back reasoning it
    encrypted (OpenAI's own state, which `accepts`, the codec's `accepts_reasoning_state` for
    `model`, lets through) and the model's own reasoning text, so reasoning with another provider's
    state, and a summary or redaction with no state, are left out. A reasoning budget is sent as the
    nearest effort level (the API has no budget). Breakpoints on tool definitions and tool calls
    have no form here. A tool result and its marked final part both get the longer of their TTLs,
    and each TTL is then fitted to one the API offers (`30m` or `12h`). A turn's tool results go
    first.
    """
    adaptation = AdaptedRequestBuilder(request)
    drop_foreign_extras(adaptation, "openai_responses")
    drop_foreign_reasoning(adaptation, accepts)
    drop_stop_sequences(adaptation)
    clear_tool_error_flags(adaptation)
    drop_assistant_blocks(
        adaptation,
        lambda block: (
            isinstance(block, ContentReasoning)
            and block.reasoning_state is None
            and block.form == "summary"
        ),
        "unsigned_reasoning",
        "reasoning summary block(s) with no OpenAI state",
    )
    drop_assistant_blocks(
        adaptation,
        lambda block: (
            isinstance(block, ContentReasoning)
            and block.reasoning_state is None
            and block.form == "redacted"
        ),
        "redacted_reasoning",
        "redacted reasoning block(s) with no OpenAI state",
    )
    budget_as_effort(adaptation)
    drop_breakpoints(
        adaptation,
        lambda value, _: isinstance(value, ToolParam | ContentToolUse),
        "on tool definitions and tool calls",
    )
    merge_tool_result_breakpoints(adaptation, _request_cache_ttl(adaptation.request, extra_body))
    fit_cache_ttls(adaptation, _CACHE_TTLS)
    tool_results_first(adaptation)
    drop_non_object_response_format(adaptation)
    move_rejected_keywords(adaptation, openai_rejects)
    move_strict_rejected_keywords(adaptation)
    unenforce_incompatible_response_format(adaptation)
    unenforce_incompatible_strict_tools(adaptation, strict_problems(None))
    return adaptation.result()


def _request_cache_ttl(request: ModelRequest, extra_body: dict[str, Any] | None) -> str:
    """The `prompt_cache_options.ttl` an encoding of `request` sends, which a breakpoint without
    a TTL inherits: the request's provider parameters and extras merged as the encoder merges
    them, then the codec's `extra_body`, then the API's default."""
    body: dict[str, Any] = {}
    merge_provider_params(body, request.sampling.extra)
    apply_dialect_extras(body, request.extras, "openai_responses")
    apply_declared_defaults(body, extra_body or {})
    options = body.get("prompt_cache_options")
    ttl = options.get("ttl") if isinstance(options, dict) else None
    return ttl if isinstance(ttl, str) else _DEFAULT_CACHE_TTL


def encode_responses_request(
    model: str,
    request: ModelRequest,
    *,
    streaming: bool,
    extra_body: dict[str, Any] | None = None,
) -> EncodedRequest:
    """Render a request as a `/v1/responses` request for `model`.

    Everything the request asked for is rendered as asked, then extras of this dialect are
    merged in (see `apply_dialect_extras`). The declared defaults, filled in only where the
    request left the key absent: `extra_body` first (the codec's configuration), then the
    built-in `store: false` and `reasoning.summary: "auto"` (when reasoning is on and
    `ReasoningParams.summary` chose nothing). An explicit summary choice overrides what a
    same-dialect extra retained unless the two are compatible (`_apply_summary_choice`).
    """
    sampling = request.sampling
    if sampling.stop_sequences:
        raise unadapted("stop sequences")
    names = ToolNames(request.tools)
    instructions, input_items = _render_context(request.context, names)
    body: dict[str, Any] = {"model": model, "input": input_items}
    if instructions is not None:
        body["instructions"] = instructions
    if request.tools:
        body["tools"] = _tools_param(request.tools)
    if request.tool_choice is not None:
        body["tool_choice"] = _tool_choice_param(request.tool_choice, names)
    if request.parallel_tool_calls is not None:
        body["parallel_tool_calls"] = request.parallel_tool_calls
    if sampling.max_tokens is not None:
        body["max_output_tokens"] = sampling.max_tokens
    if sampling.temperature is not None:
        body["temperature"] = sampling.temperature
    if sampling.top_p is not None:
        body["top_p"] = sampling.top_p
    reasoning = _reasoning_param(sampling.reasoning)
    if reasoning is not None:
        body["reasoning"] = reasoning
    if sampling.speed is not None:
        body["service_tier"] = service_tier_param(sampling.speed)
    if request.response_format is not None:
        body["text"] = {"format": response_format_flat(request.response_format)}
    if streaming:
        body["stream"] = True
    merge_provider_params(body, sampling.extra)
    headers, query = apply_dialect_extras(body, request.extras, "openai_responses")
    # Precedence: what the request says (rendered fields, provider params, extras), then the
    # codec's configured `extra_body`, then the built-in defaults.
    apply_declared_defaults(body, {"store": False, **(extra_body or {})})
    # The body shares nothing with the caller's settings (every merge deep-copies), so the
    # nested default edits a copy.
    reasoning_body = _as_dict(body.get("reasoning"))
    if reasoning_body is not None:
        if reasoning_body.get("effort") != "none":
            _apply_summary_choice(reasoning_body, sampling.reasoning)
        elif sampling.reasoning is not None and sampling.reasoning.summary is not None:
            # Reasoning disabled takes precedence over an explicit summary choice: a retained
            # control goes too. An unspecified choice keeps a control the harness sent with
            # reasoning off, so its request reconstructs as sent.
            reasoning_body.pop("summary", None)
    return EncodedRequest(
        path=_ENDPOINT, body=cast("JSONObject", body), query=query, headers=headers
    )


# --- model side: Responses SSE / response body -> ModelResponse -----------------------------------


def _model_reasoning_content(item: dict[str, Any]) -> ContentReasoning:
    """A provider's `reasoning` output item as a content block: its text (`_reasoning_form`
    says which), and its identity as the state when it carries `encrypted_content`."""
    summary, literal = _reasoning_texts(item)
    return ContentReasoning(
        text=summary or literal,
        form=_reasoning_form(summary, literal),
        reasoning_state=_model_reasoning_state(item),
    )


def _model_reasoning_state(item: dict[str, Any]) -> DynamicData | None:
    encrypted = item.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted:
        return None
    item_id = item.get("id")
    return DynamicData.from_tagged(
        OpenAIResponsesReasoning(
            id=item_id if isinstance(item_id, str) and item_id else None,
            encrypted_content=encrypted,
        )
    )


def _model_tool_use_content(item: dict[str, Any]) -> ContentToolUse:
    namespace = item.get("namespace")
    if item.get("type") == "custom_tool_call":
        return ContentToolUse(
            id=str(item.get("call_id", "")),
            name=str(item.get("name", "")),
            input={"input": str(item.get("input", ""))},
            parse_error=None,
            namespace=namespace if isinstance(namespace, str) else None,
        )
    parsed, parse_error = _parse_arguments(str(item.get("arguments") or ""))
    return ContentToolUse(
        id=str(item.get("call_id", "")),
        name=str(item.get("name", "")),
        input=cast("dict[str, Any]", parsed),
        parse_error=parse_error,
        namespace=namespace if isinstance(namespace, str) else None,
    )


def _model_stop_reason(
    response: dict[str, Any], saw_tool_call: bool, saw_refusal: bool = False
) -> StopReason:
    if response.get("status") == "incomplete":
        details = _as_dict(response.get("incomplete_details")) or {}
        reason = details.get("reason")
        if reason == "max_output_tokens":
            return StopMaxTokens()
        if reason == "content_filter":
            return StopRefusal(provider_reason="content_filter", complete=False)
        return StopOther(provider_reason=f"{_INCOMPLETE_PREFIX}{reason or 'no reason reported'}")
    if saw_refusal:
        return StopRefusal(provider_reason="refusal", complete=True)
    if saw_tool_call:
        return StopToolUse()
    return StopEndTurn()


def _model_usage(response: dict[str, Any]) -> Usage:
    usage = _as_dict(response.get("usage"))
    speed = service_tier_speed(response.get("service_tier"))
    if usage is None:
        return Usage(
            input_tokens=0,
            cache_read_input_tokens=None,
            cache_write_input_tokens=None,
            output_tokens=0,
            reasoning_output_tokens=None,
            speed=speed,
        )
    input_details = _as_dict(usage.get("input_tokens_details")) or {}
    output_details = _as_dict(usage.get("output_tokens_details")) or {}
    cached = _as_int(input_details.get("cached_tokens")) or 0
    cache_write = _as_int(input_details.get("cache_write_tokens"))
    return Usage(
        # Responses `input_tokens` is the total, cache reads and cache writes included, while
        # Usage.input_tokens is what was neither.
        input_tokens=max(
            (_as_int(usage.get("input_tokens")) or 0) - cached - (cache_write or 0), 0
        ),
        cache_read_input_tokens=cached,
        cache_write_input_tokens=cache_write,
        output_tokens=_as_int(usage.get("output_tokens")) or 0,
        reasoning_output_tokens=_as_int(output_details.get("reasoning_tokens")),
        speed=speed,
    )


def response_object_to_model_response(response: dict[str, Any]) -> ModelResponse:
    """A complete Responses `Response` object as a ModelResponse."""
    content: list[Content] = []
    saw_tool_call = False
    saw_refusal = False
    for item in _dict_list(response.get("output")):
        item_type = item.get("type")
        if item_type == "message":
            for part in _dict_list(item.get("content")):
                if part.get("type") == "output_text":
                    text = str(part.get("text", ""))
                    if text:
                        content.append(ContentText(text=text))
                elif part.get("type") == "refusal":
                    # The model's refusal in place of the answer: its text, and the response's
                    # stop reason says it was a refusal.
                    saw_refusal = True
                    text = str(part.get("refusal", ""))
                    if text:
                        content.append(ContentText(text=text))
        elif item_type == "reasoning":
            content.append(_model_reasoning_content(item))
        elif item_type in _CALL_TYPES:
            saw_tool_call = True
            content.append(_model_tool_use_content(item))
        else:
            logger.warning("Ignoring unsupported Responses output item type %r", item_type)
    return ModelResponse(
        id=str(response.get("id", "")),
        output=(AssistantMessage(content=tuple(content)),),
        stop_reason=_model_stop_reason(response, saw_tool_call, saw_refusal),
        usage=_model_usage(response),
    )


class ResponsesSseDecoder:
    """Decodes a Responses SSE stream into stream events and the assembled response.

    The terminal `response.completed` / `response.incomplete` event carries the fully assembled
    `Response`, so assembly reads that; delta events are only relayed. Tool-call ordinals are
    assigned in arrival order and keyed by the `fc_...` item id so argument deltas can be
    correlated. `response.failed` and `error` events end the stream as a failure.

    A reasoning item's block starts at its first text delta, whose kind (summary or literal
    reasoning text) gives the block's form, or at the item's end when it streamed no text; its
    state follows when the item is done.
    """

    def __init__(self) -> None:
        self._frames = SseFrameParser()
        self._started_reasoning: set[str] = set()  # reasoning item ids whose block has started
        self._tool_ordinals: dict[str, int] = {}
        self._response: dict[str, Any] | None = None
        self._failure: ModelCallFailure | None = None

    def _handle(self, event: dict[str, Any]) -> list[StreamEvent]:
        event_type = event.get("type")
        delta = event.get("delta")
        delta = delta if isinstance(delta, str) else ""
        if event_type == "response.output_text.delta":
            return [StreamTextDelta(text=delta)] if delta else []
        if event_type == "response.refusal.delta":
            # Refusal text streams as text; the final response's stop reason marks it.
            return [StreamTextDelta(text=delta)] if delta else []
        if event_type in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            if not delta:
                return []
            form: ReasoningForm = (
                "summary" if event_type == "response.reasoning_summary_text.delta" else "full"
            )
            return [
                *self._start_reasoning(str(event.get("item_id", "")), form),
                StreamReasoningDelta(text=delta),
            ]
        if event_type == "response.function_call_arguments.delta":
            ordinal = self._tool_ordinals.get(str(event.get("item_id", "")))
            if ordinal is None or not delta:
                return []
            return [StreamToolUseInputDelta(index=ordinal, partial_json=delta)]
        item = _as_dict(event.get("item")) or {}
        if event_type == "response.output_item.added":
            if item.get("type") in _CALL_TYPES:
                ordinal = len(self._tool_ordinals)
                self._tool_ordinals[str(item.get("id") or f"fc_ordinal{ordinal}")] = ordinal
                namespace = item.get("namespace")
                return [
                    StreamToolUseStart(
                        index=ordinal,
                        id=str(item.get("call_id", "")),
                        name=str(item.get("name", "")),
                        namespace=namespace if isinstance(namespace, str) else None,
                    )
                ]
            return []
        if event_type == "response.output_item.done":
            if item.get("type") == "custom_tool_call":
                # Freeform text is not JSON, so it is relayed whole, as the one-field object
                # the vocabulary prescribes, once the item is complete.
                ordinal = self._tool_ordinals.get(str(item.get("id", "")))
                if ordinal is None:
                    return []
                return [
                    StreamToolUseInputDelta(
                        index=ordinal, partial_json=json.dumps({"input": item.get("input", "")})
                    )
                ]
            if item.get("type") != "reasoning":
                return []
            summary, literal = _reasoning_texts(item)
            out = self._start_reasoning(str(item.get("id", "")), _reasoning_form(summary, literal))
            state = _model_reasoning_state(item)
            if state is not None:
                out.append(StreamReasoningState(state=state))
            return out
        if event_type in ("response.completed", "response.incomplete"):
            self._response = _as_dict(event.get("response"))
            return []
        if event_type == "response.failed":
            response = _as_dict(event.get("response")) or {}
            error = _as_dict(response.get("error"))
            self._failure = failure_from_error_object(
                error if error is not None else {"message": "response.failed with no error"}
            )
            return []
        if event_type == "error":
            self._failure = failure_from_error_object(
                {"message": event.get("message") or "error", "code": event.get("code")}
            )
            return []
        return []

    def _start_reasoning(self, item_id: str, form: ReasoningForm) -> list[StreamEvent]:
        """The start of a reasoning item's block, the first time the item is seen."""
        if item_id in self._started_reasoning:
            return []
        self._started_reasoning.add(item_id)
        return [StreamReasoningStart(form=form)]

    def _consume(self, frames: list[tuple[str | None, str]]) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for _event, data in frames:
            if self._failure is not None:
                continue
            if data.strip() == "[DONE]":
                # OpenAI ends a Responses stream at `response.completed`; some compatible servers
                # follow it with chat completions' terminator, as the SDK tolerates.
                continue
            events.extend(self._handle(decode_json_object(data, "Responses stream event")))
        return events

    def feed(self, chunk: bytes) -> list[StreamEvent]:
        return self._consume(self._frames.feed(chunk))

    def complete(self) -> bool:
        return self._response is not None or self._failure is not None

    def finish(self) -> ModelResponse | ModelCallFailure:
        self._consume(self._frames.finish())
        if self._failure is not None:
            return self._failure
        if self._response is None:
            return ModelCallFailure(
                category="server_error",
                description="Responses stream ended without a terminal response",
                provider_http_status=None,
            )
        return response_object_to_model_response(self._response)

    def raw_message(self) -> JSONObject | None:
        if self._failure is not None or self._response is None:
            return None
        return cast("JSONObject", self._response)


def decode_responses_response(body: bytes) -> ModelResponse | ModelCallFailure:
    """A complete (non-streamed) `Response` body as a ModelResponse."""
    response = decode_json_object(body, "Responses body")
    error = _as_dict(response.get("error"))
    if error is not None:
        return failure_from_error_object(error)
    return response_object_to_model_response(response)


def decode_responses_failure(status: int, body: bytes) -> ModelCallFailure:
    return decode_failure_body(status, body)
