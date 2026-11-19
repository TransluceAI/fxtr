"""Google Gemini `generateContent` ⇄ behaviors model vocabulary translation: the
`GeminiGenerateContentCodec`.

Pure functions and a codec object binding them to the `DialectCodec` protocol; no I/O and no
third-party imports. The dialect is the Generative Language API's generateContent family
(`POST /v1beta/models/{model}:generateContent` and `:streamGenerateContent?alt=sse`), spoken by
the google-genai SDKs and so by Gemini CLI, and by Antigravity CLI on its API-key route. Unlike the
Anthropic/Responses dialects, the model name and the method live in the request *path* (parsed by
`parse_gemini_target`), not the body, and streaming is signaled by the method and the `alt=sse`
query parameter, never a body field. The harness side (serving a harness that speaks the dialect) is
documented here; the model side (`GeminiGenerateContentAPI` calling the
Gemini API: `encode_gemini_request`, `GeminiSseDecoder`, `decode_gemini_response`,
`decode_gemini_failure`) at its own section further down. Harness side:

* **Request** (`gemini_request_to_canonical`): a generateContent request body → a
  `DecodedRequest`. `systemInstruction` folds into a leading `SystemMessage`; each
  `functionResponse` part becomes its own `ToolMessage` (`{"error": ...}` an error result). Gemini
  pairs function responses to calls by name and order where the model vocabulary pairs strictly
  by id, so ids are synthesized where a call has none (`_ToolCallIds`, recognizably: the encoder
  leaves them out again). A model turn's thought parts are summarized reasoning, and a
  `thoughtSignature` is the reasoning state of the reasoning block placed just before the block of
  the part it rode on (`model_blocks`, `GeminiThoughtSignature`).
* **Response**: `synthesize_gemini_sse` turns a subject-model stream (`StreamEvent` deltas plus
  the terminal `ModelResponse` or `ModelCallFailure`) into the `streamGenerateContent?alt=sse`
  frame sequence (bare `data:` lines carrying whole `GenerateContentResponse` objects — no
  `event:` line and no `[DONE]` sentinel); `response_to_gemini_response` builds the non-streaming
  body, its blocks rendered as parts by the same `model_parts` the model side uses.
  `gemini_failure_body` renders a request-time `ModelCallFailure` as the dialect's status + error
  body.

Request coverage: what the decoder does with each field the request schema allows (models it,
keeps it as an extra for a same-dialect subject, or drops it with a recorded loss), and how the
reconstruction check compares it, is tabulated per schema path in
`coverage/gemini_generate_content.yaml`, generated from a snapshot of the API's Discovery document
by `tests/dialect_coverage`; a test fails when the codec stops matching it. The notes below cover
what it cannot show: the response side, and the design choices behind some of its rows.

Known fidelity weak spots:

* **Reasoning Gemini has no form for travels in bundles.** Gemini carries one signature per
  model response, and the harnesses rebuild their history around that: Antigravity CLI merges a
  response's thought text and concatenates its signatures onto one part, and Gemini CLI drops
  thought parts and keeps a merged run of text's first signature only. So unless a response's
  reasoning is in Gemini's own shape (summaries, with at most one Gemini signature among them),
  it goes to the harness in bundles (`_reasoning_envelope.envelope_bytes`: each block's form,
  text, state and position), with its text also sent as unsigned thought parts for display
  (`_bundled_parts`). Reasoning before text is bundled on that text's first part; the rest rides
  the first function call, where Gemini puts a response's signature, or with no call a trailing
  empty part (which Gemini CLI drops). The request decoder rebuilds the blocks from the bundles
  and places them by position, dropping the display parts (`model_blocks`). Positions are kept
  between text and tool calls, not inside a run of text: a harness may merge adjacent text, so
  reasoning between two texts comes back before both. A response that mixes a Gemini signature
  with other reasoning carries that signature in a bundle too, so a harness resumed directly
  against Gemini loses it; Gemini never produces such a response. Only Gemini's own
  signatures reach the Gemini API, and only for the model they were issued for
  (`accepts_reasoning_state`).
* **Streamed usage arrives only on the final frame.** The real API reports running
  `usageMetadata` on every frame; usage arrives assembled on the terminal `ModelResponse`, so
  earlier frames carry none.
* **Tool-call args are buffered, not streamed, and so is what follows them.** A Gemini
  `functionCall` part always carries the complete `args` object (there is no partial-args delta
  form), so the parts are built from the terminal response's assembled `ContentToolUse.input` and
  ride the final frame, with their signatures; text or reasoning after the first call waits for
  that frame too, so the harness gets the response in its order.
* **One candidate.** A response's `candidates[0]` is decoded; a request for more
  (`candidateCount` above one) is a recorded loss, since a subject of another dialect answers
  once.
* **A signature streamed for the thought part it rode on** arrives after that part's text has
  streamed, so it rides an empty thought part of its own; the assembled response keeps it with
  its text, and so does the request decoder when the harness echoes the parts as they streamed.
* **Mid-stream failures surface as a final `{"error": ...}` data frame.** The real API has no
  in-stream failure event; google-genai clients raise when a frame parses to an error. Failures
  with no prior deltas get real HTTP statuses from the backend as usual.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import urllib.parse
import uuid
from collections import deque
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from behaviors.implementations.dialects._common import (
    BodyReader,
    LossLog,
    ReasoningStateFilter,
    ToolNames,
    apply_dialect_extras,
    failure_message,
    has_header,
    loads_strict_json,
    merge_provider_params,
    request_extras,
    unadapted,
    without_reasoning,
)
from behaviors.implementations.dialects._openai_common import (
    decode_failure_body,
    failure_from_error_object,
)
from behaviors.implementations.dialects._reasoning_envelope import (
    BundledReasoning,
    envelope_bytes,
    read_bundles,
)
from behaviors.implementations.dialects.adaptation import (
    AdaptedRequestBuilder,
    breakpoint_of,
    drop_assistant_blocks,
    drop_breakpoints,
    drop_foreign_extras,
    drop_foreign_reasoning,
    drop_speed,
    later_system_messages_as_user,
    note_flattened_tools,
    replace_reasoning,
)
from behaviors.implementations.dialects.codec_utils import (
    SseFrameParser,
    decode_json_object,
    own_reasoning_state,
)
from behaviors.implementations.dialects.contract_adaptation import (
    drop_output_schemas,
    enforce_response_format,
    fit_strict_tools_to_gemini,
    move_rejected_keywords,
    note_unenforced_response_keywords,
    resolve_automatic_strictness,
)
from behaviors.implementations.dialects.schema_support import gemini_rejects
from behaviors.interfaces.dialect_codecs import (
    AdaptedRequest,
    DecodedRequest,
    Route,
    RoutingError,
    SseDecoder,
    StopRendering,
)
from behaviors.types.adaptation import ResponseLoss
from behaviors.types.content import (
    Content,
    ContentReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.dialect import Dialect, EncodedRequest, Headers
from behaviors.types.failure import FailureCategory, ModelCallFailure
from behaviors.types.json import JSONObject
from behaviors.types.message import (
    AssistantMessage,
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
    ResponseFormatText,
    SamplingParams,
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
    ToolChoice,
    ToolChoiceAuto,
    ToolChoiceNone,
    ToolChoiceRequired,
    ToolChoiceTool,
    ToolParam,
)
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData

GeminiSignaturePlacement = Literal["thought", "next"]
"""Where a Gemini signature rode: on the thought part whose text is the reasoning block's
(`thought`), or on the part after the reasoning (`next`: a function call, the response text)."""


@dataclass(frozen=True)
class GeminiThoughtSignature(
    TaggedDataclass, fxtr_type=behaviors_type("GeminiThoughtSignature.v1")
):
    """A Gemini part's `thoughtSignature`, and the part it rode on.

    The API attaches a signature to the part it belongs with, which may be a thought part (a
    summary, `thought: true`) or the part after the thinking. The reasoning block sits just before
    the block the signature rode on, so a codec rendering Gemini puts it back on the thought part
    the block's text is, or on the next part.

    Attributes:
        model: The requested model id, without a `models/` prefix: a Gemini codec sends the
            signature only to the same model.
        signature: The signature, base64 as the API sends it.
        on: Which part it rode on.
    """

    model: str
    signature: str
    on: GeminiSignaturePlacement


logger = logging.getLogger(__name__)

# --- request target: /v1beta/models/{model}:{method} --------------------------------------------

_KNOWN_METHODS = frozenset(
    {"generateContent", "streamGenerateContent", "countTokens", "embedContent"}
)
_TARGET_RE = re.compile(r"/models/(?P<model>[^:/]+):(?P<method>[A-Za-z]+)$")


@dataclass(frozen=True)
class GeminiTarget:
    """A parsed Gemini API request target: `/v1beta/models/{model}:{method}[?alt=sse]`."""

    model: str
    method: str
    alt_sse: bool


def parse_gemini_target(target: str) -> GeminiTarget | None:
    """Parse a request target as a Gemini generateContent-family endpoint, or None.

    Matches `/models/{model}:{method}` under any version prefix (`/v1beta`, `/v1`, ...) where
    `method` is one of the generateContent-family methods. `alt_sse` reflects the `alt=sse` query
    parameter — how the google-genai SDK requests SSE framing for streaming calls.
    """
    path, _, query = target.partition("?")
    path = path.rstrip("/")
    match = _TARGET_RE.search(path)
    if match is None or match.group("method") not in _KNOWN_METHODS:
        return None
    alt = urllib.parse.parse_qs(query).get("alt", [])
    return GeminiTarget(
        model=match.group("model"), method=match.group("method"), alt_sse="sse" in alt
    )


# --- request: generateContent body -> DecodedRequest ----------------------------------------------


def gemini_request_to_canonical(
    target: str, body: dict[str, Any], headers: Headers = ()
) -> DecodedRequest:
    """Translate a parsed generateContent request into a `DecodedRequest`.

    `target` is the raw request target — the model name lives in the path, not the body. What
    the vocabulary has no slot for is attached as `DialectExtras` (`ModelRequest.extras`): the
    leftover leaves of `generationConfig` (`topK`, `thinkingConfig.includeThoughts`, a response
    MIME type the vocabulary has no form for, ...),
    every top-level key the decoder does not read (`safetySettings`, `cachedContent`, ...),
    unknown headers, and the query minus the credential and the `alt` framing parameter.
    """
    parsed = parse_gemini_target(target)
    model = parsed.model if parsed is not None else ""
    losses = LossLog()
    reader = BodyReader(body)
    tools = _convert_tools(reader.take("tools"), losses)
    context = _convert_contents(
        reader.take("contents"),
        reader.take("systemInstruction"),
        model,
        losses,
        frozenset(tool.name for tool in tools if tool.output_schema is not None),
    )
    tool_choice, tools = _convert_tool_config(reader, tools, losses)
    candidates = reader.peek("generationConfig", "candidateCount")
    if isinstance(candidates, int) and candidates > 1:
        # Alternatives the vocabulary's response has no slot for: a subject of another dialect
        # answers once (the setting stays in the extras for a Gemini subject).
        losses.note("candidates", f"candidateCount {candidates}")
    sampling = SamplingParams(
        max_tokens=_take_int(reader, "generationConfig", "maxOutputTokens"),
        temperature=_take_float(reader, "generationConfig", "temperature"),
        top_p=_take_float(reader, "generationConfig", "topP"),
        stop_sequences=_take_str_list(reader, "generationConfig", "stopSequences"),
        reasoning=_convert_reasoning(reader),
    )
    request = ModelRequest(
        context=tuple(context),
        tools=tuple(tools),
        tool_choice=tool_choice,
        parallel_tool_calls=None,
        sampling=sampling,
        response_format=_convert_response_format(reader, losses),
        extras=request_extras(
            "gemini_generate_content", reader, headers, target, consumed_query=("alt",)
        ),
    )
    return DecodedRequest(
        model=model,
        request=request,
        streaming=parsed is not None and parsed.method == "streamGenerateContent",
        decode_losses=losses.recorded(),
    )


# A `tools[]` entry is an object keyed by tool kind; `functionDeclarations` are the harness's
# own tools, `computerUse` is a tool the harness runs, the rest run at the provider.
_PROVIDER_TOOL_KEYS: dict[str, ProviderToolFamily] = {
    "googleSearch": "web_search",
    "googleSearchRetrieval": "web_search",
    "urlContext": "web_fetch",
    "codeExecution": "code_execution",
    "fileSearch": "file_search",
    "mcpServers": "mcp",
    "googleMaps": "unknown",
}
_CLIENT_TOOL_KEYS = frozenset({"functionDeclarations", "computerUse"})


def provider_tools_in_request(body: dict[str, Any]) -> tuple[ProviderTool, ...]:
    """The provider-side tools a generateContent body declares: every key of a `tools[]` entry
    other than the harness's own function declarations (see `_PROVIDER_TOOL_KEYS`)."""
    out: list[ProviderTool] = []
    for tool in _dict_list(body.get("tools")):
        for key in tool:
            if key in _CLIENT_TOOL_KEYS:
                continue
            out.append(ProviderTool(family=_PROVIDER_TOOL_KEYS.get(key, "unknown"), name=key))
    return tuple(out)


def _take_int(reader: BodyReader, key: str, *path: str) -> int | None:
    value = _as_int(reader.peek(key, *path))
    if value is not None:
        reader.take_leaf(key, *path)
    return value


def _take_float(reader: BodyReader, key: str, *path: str) -> float | None:
    value = _as_float(reader.peek(key, *path))
    if value is not None:
        reader.take_leaf(key, *path)
    return value


def _take_str_list(reader: BodyReader, key: str, *path: str) -> tuple[str, ...]:
    raw = reader.peek(key, *path)
    if not isinstance(raw, list):
        return ()
    reader.take_leaf(key, *path)
    return _str_list(raw)


def _convert_reasoning(reader: BodyReader) -> ReasoningParams | None:
    """The request's `thinkingConfig` as neutral reasoning params.

    `thinkingBudget` maps natively: `0` is off, `-1` (or absent) is dynamic, enabled with the
    provider choosing depth, and a positive budget is a budget. `thinkingLevel` (the Gemini 3
    control; the API refuses it beside a budget) is the effort, and `includeThoughts` whether
    thought summaries come back.
    """
    if not isinstance(reader.peek("generationConfig", "thinkingConfig"), dict):
        return None
    budget = _take_int(reader, "generationConfig", "thinkingConfig", "thinkingBudget")
    level = reader.peek("generationConfig", "thinkingConfig", "thinkingLevel")
    effort: ReasoningEffort | None = None
    if isinstance(level, str) and level.lower() in _THINKING_LEVELS:
        reader.take_leaf("generationConfig", "thinkingConfig", "thinkingLevel")
        effort = cast("ReasoningEffort", level.lower())
    include = reader.peek("generationConfig", "thinkingConfig", "includeThoughts")
    summary: bool | None = None
    if isinstance(include, bool):
        reader.take_leaf("generationConfig", "thinkingConfig", "includeThoughts")
        summary = include
    if budget == 0:
        return ReasoningParams(enabled=False, summary=summary)
    if budget is not None and budget > 0:
        return ReasoningParams(budget_tokens=budget, effort=effort, summary=summary)
    if budget == -1:
        # Dynamic is the default; the encoder leaves it unsaid, and the check reads it so.
        pass
    return ReasoningParams(effort=effort, summary=summary)


_THINKING_LEVELS = ("minimal", "low", "medium", "high")


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


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    return float(value) if isinstance(value, (int, float)) else None


# Ids the codec makes up for function calls that arrive without one. An encoder rendering this
# dialect leaves them out again, so a history pairs by name and order as it came.
SYNTHETIC_CALL_ID_PREFIX = "gemini_call_"


def is_synthetic_call_id(call_id: str) -> bool:
    return call_id.startswith(SYNTHETIC_CALL_ID_PREFIX)


class _ToolCallIds:
    """Synthesizes stable ids for Gemini function calls.

    Gemini pairs `functionResponse` parts to `functionCall`s by name and order (the optional wire
    `id` is normally absent); the model vocabulary pairs strictly by id. Ids are synthesized
    deterministically in document order, so the same replayed history translates to the same ids
    on every request — keeping multi-turn histories self-consistent from the provider's point of
    view. Explicit ids, when a client does send them, win.
    """

    def __init__(self) -> None:
        self._counter = 0
        self._orphans = 0
        self._pending: dict[str, deque[str]] = {}

    def call_id(self, explicit: Any, name: str) -> str:
        call_id = (
            explicit
            if isinstance(explicit, str) and explicit
            else f"{SYNTHETIC_CALL_ID_PREFIX}{self._counter}"
        )
        self._counter += 1
        self._pending.setdefault(name, deque()).append(call_id)
        return call_id

    def response_id(self, explicit: Any, name: str) -> str:
        queue = self._pending.get(name)
        if isinstance(explicit, str) and explicit:
            if queue is not None and explicit in queue:
                queue.remove(explicit)
            return explicit
        if queue:
            return queue.popleft()
        self._orphans += 1
        return f"{SYNTHETIC_CALL_ID_PREFIX}orphan_{self._orphans}"


def _convert_contents(
    contents: Any,
    system_instruction: Any,
    model: str,
    losses: LossLog,
    output_schema_tools: frozenset[str] = frozenset(),
) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    system_parts = _instruction_parts(system_instruction, losses)
    if system_parts:
        out.append(SystemMessage(content=system_parts))
    ids = _ToolCallIds()
    for content in _dict_list(contents):
        if content.get("role") == "model":
            blocks = model_blocks(content.get("parts"), model, ids, losses)
            out.append(AssistantMessage(content=tuple(blocks)))
        else:
            out.extend(_convert_user_content(content, ids, losses, output_schema_tools))
    return out


def _instruction_parts(system_instruction: Any, losses: LossLog) -> tuple[ContentText, ...]:
    """`systemInstruction` (a Content object, or a bare string some clients send) as text blocks,
    one per part; its non-text parts go through the same leaf handling as every other part."""
    if isinstance(system_instruction, str):
        return (ContentText(text=system_instruction),) if system_instruction else ()
    data = _as_dict(system_instruction)
    if data is None:
        return ()
    return tuple(ContentText(text=text) for text in _leaf_texts(data.get("parts"), losses))


def gemini_model_id(model: str) -> str:
    """A Gemini model name without the `models/` resource prefix some clients put on it."""
    return model.removeprefix("models/")


def _gemini_state(model: str, signature: str, on: GeminiSignaturePlacement) -> DynamicData:
    return DynamicData.from_tagged(
        GeminiThoughtSignature(model=gemini_model_id(model), signature=signature, on=on)
    )


def _content_groups(blocks: Sequence[Content]) -> list[int | None]:
    """For each block, the group of the response's non-reasoning content it belongs to, or None
    for reasoning: each tool call is a group, and so is each run of text that only reasoning
    interrupts (a harness may merge text once the reasoning between it is gone)."""
    groups: list[int | None] = []
    group, last_was_text = -1, False
    for block in blocks:
        if isinstance(block, ContentReasoning):
            groups.append(None)
            continue
        is_text = isinstance(block, ContentText)
        if not (is_text and last_was_text):
            group += 1
        last_was_text = is_text
        groups.append(group)
    return groups


def _bundle(blocks: Sequence[Content]) -> list[BundledReasoning]:
    """The reasoning of a response as a bundle: each block with the group it precedes."""
    groups = _content_groups(blocks)
    total = max((g for g in groups if g is not None), default=-1) + 1
    bundle: list[BundledReasoning] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, ContentReasoning):
            continue
        at = next((g for g in groups[index + 1 :] if g is not None), total)
        bundle.append(BundledReasoning(at, block.form, block.text, block.reasoning_state))
    return bundle


def _needs_bundle(blocks: Sequence[Content]) -> bool:
    """Whether a response's reasoning must travel to a harness as a bundle: unless it is in
    Gemini's own shape, summaries with at most one Gemini signature among them."""
    signatures = 0
    for block in blocks:
        if not isinstance(block, ContentReasoning):
            continue
        if block.form != "summary":
            return True
        if block.reasoning_state is not None:
            if own_reasoning_state(block.reasoning_state, GeminiThoughtSignature) is None:
                return True
            signatures += 1
    return signatures > 1


def _bundled_reasoning(parts: Any) -> list[BundledReasoning] | None:
    """The reasoning bundled in a model content's signatures, in order; None when no signature
    is a bundle."""
    found: list[BundledReasoning] | None = None
    for part in _dict_list(parts):
        signature = part.get("thoughtSignature")
        if not isinstance(signature, str) or not signature:
            continue
        blocks = read_bundles(signature)
        if blocks is not None:
            found = [*(found or []), *blocks]
    return found


def _blocks_with_bundled_reasoning(
    parts: Any, bundled: list[BundledReasoning], ids: _ToolCallIds, losses: LossLog
) -> list[Content]:
    """A model content's blocks when its reasoning came back as a bundle: its text and calls in
    order, its thought parts dropped as the display copies they are, and each bundled block
    placed before the group of content its position names (`_content_groups`)."""
    content: list[Content] = []
    for part in _dict_list(parts):
        call = _as_dict(part.get("functionCall"))
        is_text = "text" in part and not any(kind in part for kind in _PART_KINDS if kind != "text")
        consumed = {"thoughtSignature"} if is_text or call is not None else set[str]()
        if is_text and part.get("thought") is True:
            consumed.add("thought")
            _note_part_fields(part, consumed, losses)
            continue
        _note_part_fields(part, consumed, losses)
        if call is not None:
            name = str(call.get("name", ""))
            content.append(
                ContentToolUse(
                    id=ids.call_id(call.get("id"), name),
                    name=name,
                    input=_as_dict(call.get("args")) or {},
                    parse_error=None,
                )
            )
        elif is_text:
            if text := str(part.get("text", "")):
                content.append(ContentText(text=text))
        else:
            _count_dropped_part(part, losses)
    groups = _content_groups(content)
    out: list[Content] = []
    remaining = sorted(bundled, key=lambda entry: entry.at)
    for block, group in zip(content, groups, strict=True):
        assert group is not None
        while remaining and remaining[0].at <= group:
            out.append(_reasoning_of(remaining.pop(0)))
        out.append(block)
    out.extend(_reasoning_of(entry) for entry in remaining)
    return out


def _reasoning_of(entry: BundledReasoning) -> ContentReasoning:
    return ContentReasoning(text=entry.text, form=entry.form, reasoning_state=entry.state)


def model_blocks(
    parts: Any, model: str, ids: _ToolCallIds, losses: LossLog, *, join: bool = False
) -> list[Content]:
    """A model content's parts as vocabulary blocks, in order.

    A thought part (`thought: true`) is summarized reasoning. A `thoughtSignature` is the
    reasoning state of a reasoning block placed just before the block of the part it rode on: the
    thought part itself (the block is that part's summary), or the part after the thinking (a
    function call, the response text), whose block follows; a signature on an empty text part
    (where a streamed response ends) has no block of its own to precede. Thought text that no
    signature closes is unsigned reasoning. A content whose signatures hold a bundle (reasoning a
    harness-side rendering could not carry as one Gemini signature) is read from the bundle
    instead (`_blocks_with_bundled_reasoning`).

    `join` merges consecutive text fragments of one kind (a streamed response arrives in pieces);
    a request's parts stay one block each, except that an empty thought part carrying a signature
    belongs to the thought text before it: that is how a streamed thought reaches a harness, its
    signature arriving after its text.
    """
    bundled = _bundled_reasoning(parts)
    if bundled is not None:
        return _blocks_with_bundled_reasoning(parts, bundled, ids, losses)
    out: list[Content] = []
    pending: list[str] = []  # thought text of the reasoning block still open

    def close(signature: str | None, placement: GeminiSignaturePlacement) -> None:
        if not pending and signature is None:
            return
        text = "".join(pending)
        pending.clear()
        if signature is None:
            out.append(ContentReasoning(text=text, form="summary"))
            return
        out.append(
            ContentReasoning(
                text=text,
                form="summary",
                reasoning_state=_gemini_state(model, signature, placement),
            )
        )

    for part in _dict_list(parts):
        call = _as_dict(part.get("functionCall"))
        is_text = "text" in part and not any(kind in part for kind in _PART_KINDS if kind != "text")
        thought = is_text and part.get("thought") is True
        convertible = is_text or call is not None
        raw_signature = part.get("thoughtSignature")
        signature = (
            raw_signature
            if convertible and isinstance(raw_signature, str) and raw_signature
            else None
        )
        consumed = {"thoughtSignature"} if convertible else set[str]()
        if thought:
            consumed.add("thought")
        _note_part_fields(part, consumed, losses)
        if thought:
            text = str(part.get("text", ""))
            if signature is not None and not text and pending:
                close(signature, "thought")
                continue
            if not join:
                close(None, "thought")
            pending.append(text)
            if signature is not None:
                close(signature, "thought")
            continue
        if not convertible:
            # A part the vocabulary has no slot for goes, signature and all.
            close(None, "thought")
            _count_dropped_part(part, losses)
            continue
        close(signature, "next")
        if call is not None:
            # A container whose children are data (`args`), not content.
            name = str(call.get("name", ""))
            out.append(
                ContentToolUse(
                    id=ids.call_id(call.get("id"), name),
                    name=name,
                    input=_as_dict(call.get("args")) or {},
                    parse_error=None,
                )
            )
            continue
        text = str(part.get("text", ""))
        if not text:
            continue
        previous = out[-1] if out else None
        if join and signature is None and type(previous) is ContentText:
            out[-1] = ContentText(text=previous.text + text)
        else:
            out.append(ContentText(text=text))
    close(None, "thought")
    return out


def _convert_user_content(
    content: dict[str, Any],
    ids: _ToolCallIds,
    losses: LossLog,
    output_schema_tools: frozenset[str] = frozenset(),
) -> list[ChatMessage]:
    """A user content as messages, in its parts' order: each `functionResponse` part a
    `ToolMessage`, each run of other parts a `UserMessage` with a block per text part. (A subject
    of another dialect may need a turn's results first; the backend moves them on the translated
    path, `tool_results_first`.) A response from a function in `output_schema_tools` (one whose
    declaration has an output schema) is the whole `response` object as JSON, the value that
    schema describes."""
    out: list[ChatMessage] = []
    texts: list[ContentText] = []

    def flush() -> None:
        if texts:
            out.append(UserMessage(content=tuple(texts)))
            texts.clear()

    for part in _dict_list(content.get("parts")):
        response = _as_dict(part.get("functionResponse"))
        if response is not None:
            # A container: `response` is the result data; `parts` are content (a multimodal
            # result), handled like every other part so their media is recorded when dropped.
            _note_part_fields(part, set(), losses)
            if response.get("willContinue") is not None or response.get("scheduling") is not None:
                # One of several responses to a NON_BLOCKING call (asynchronous function calling).
                losses.count("async_tool_calling", "function response(s) to a non-blocking call")
            name = str(response.get("name", ""))
            result, is_error = _function_response_text(
                response.get("response"), losses, whole=name in output_schema_tools
            )
            result += "".join(_leaf_texts(response.get("parts"), losses))
            flush()
            out.append(
                ToolMessage(
                    content=(ContentText(text=result),),
                    tool_call_id=ids.response_id(response.get("id"), name),
                    is_error=is_error,
                )
            )
            continue
        text = _leaf_text(part, losses)
        if text is not None:
            texts.append(ContentText(text=text))
    flush()
    return out


def _leaf_texts(parts: Any, losses: LossLog) -> list[str]:
    """The texts of a parts list, every other leaf recorded as dropped."""
    out: list[str] = []
    for part in _dict_list(parts):
        text = _leaf_text(part, losses)
        if text is not None:
            out.append(text)
    return out


def _leaf_text(part: dict[str, Any], losses: LossLog) -> str | None:
    """One content part that is not a function call or response: its text, or None for a part
    the vocabulary has no slot for, which is recorded on `losses` as it is dropped. Every
    container in the dialect (a message, a system instruction, a function response) sends its
    parts here, so a child is either converted or reported."""
    is_text = "text" in part and not any(kind in part for kind in _PART_KINDS if kind != "text")
    thought = is_text and part.get("thought") is True
    _note_part_fields(part, {"thought"} if thought else set[str](), losses)
    if thought:
        # Reasoning outside a model turn: no model turn to carry it, no signature to keep.
        losses.count("part:thought", "thought part(s) outside a model turn")
        return None
    if is_text:
        return str(part.get("text", ""))
    _count_dropped_part(part, losses)
    return None


# The kinds of data a part is one of (a oneof); every other field of a part is about it.
_PART_KINDS = (
    "text",
    "inlineData",
    "fileData",
    "functionCall",
    "functionResponse",
    "executableCode",
    "codeExecutionResult",
    "toolCall",
    "toolResponse",
)


def _note_part_fields(part: dict[str, Any], consumed: set[str], losses: LossLog) -> None:
    """Record the fields of a part beside its data that the vocabulary has no slot for (media
    settings, transcription, custom metadata; a signature or thought flag where nothing carries
    it), other than those the caller carries (`consumed`)."""
    for key in part:
        if key not in _PART_KINDS and key not in consumed:
            losses.count(f"part_field:{key}", f"'{key}' on content part(s)")


def _count_dropped_part(part: dict[str, Any], losses: LossLog) -> None:
    """Record a content part the vocabulary has no slot for, at the point it is dropped."""
    inline = _as_dict(part.get("inlineData"))
    if inline is not None:
        mime = str(inline.get("mimeType", ""))
        if mime.startswith("image/"):
            losses.count("image", "inline image part(s)")
        elif mime.startswith("audio/"):
            losses.count("audio", "inline audio part(s)")
        else:
            losses.count("document", "inline data part(s)")
    elif "fileData" in part:
        losses.count("document", "file data part(s)")
    elif "executableCode" in part or "codeExecutionResult" in part:
        losses.count("provider_tool_exchange", "code-execution part(s) in the history")
    elif "toolCall" in part or "toolResponse" in part:
        losses.count("provider_tool_exchange", "server-side tool call/response part(s)")
    else:
        keys = ",".join(sorted(k for k in part if k in _PART_KINDS)) or "empty"
        losses.count(f"part:{keys}", f"'{keys}' part(s)")


def _function_response_text(
    response: Any, losses: LossLog, *, whole: bool = False
) -> tuple[str, bool | None]:
    """A `functionResponse.response` object as tool-result text, and whether it is an error.

    The API's convention is `{"output": ...}` for a result and `{"error": ...}` for a failure
    (what Gemini CLI's tools produce): an `error` key makes the result an error whatever else the
    object holds, and a lone `output` or `error` string is the text. Any other value is passed
    through JSON-encoded (a lone key's value without its key, unless `whole`, for a function
    whose declared output schema describes the object itself): the vocabulary's result is text,
    so the structure is a recorded loss.
    """
    data = _as_dict(response)
    if data is None:
        return ("" if response is None else json.dumps(response)), None
    is_error = True if "error" in data else None
    if whole and not is_error:
        losses.count("structured_tool_result", "function response(s) with structured content")
        return json.dumps(data), None
    if len(data) == 1:
        ((key, value),) = data.items()
        if key in ("output", "error"):
            if isinstance(value, str):
                return value, is_error
            losses.count("structured_tool_result", "function response(s) with structured content")
            return json.dumps(value), is_error
    losses.count("structured_tool_result", "function response(s) with structured content")
    return json.dumps(data), is_error


def _convert_tools(tools: Any, losses: LossLog) -> list[ToolParam]:
    out: list[ToolParam] = []
    for tool in _dict_list(tools):
        for decl in _dict_list(tool.get("functionDeclarations")):
            name = decl.get("name")
            if not isinstance(name, str):
                continue
            if decl.get("behavior") == "NON_BLOCKING":
                # The model goes on while the call runs (asynchronous function calling).
                losses.note("async_tool_calling", f"function {name} declared NON_BLOCKING")
            description = decl.get("description")
            out.append(
                ToolParam(
                    name=name,
                    description=description if isinstance(description, str) else None,
                    input_schema=_declaration_parameters(decl, losses),
                    strict=False,
                    input_examples=None,
                    output_schema=_declaration_response(decl, losses),
                )
            )
        for key in tool:
            if key == "functionDeclarations":
                continue
            if key in _PROVIDER_TOOL_KEYS:
                losses.note(
                    f"provider_tool:{_PROVIDER_TOOL_KEYS[key]}", f"provider-side tool {key}"
                )
            elif key in _CLIENT_TOOL_KEYS:
                losses.note("builtin_tool", f"the {key} tool")
            else:
                losses.note("provider_tool:unknown", f"unrecognized tool {key}")
    return out


def _declaration_parameters(decl: dict[str, Any], losses: LossLog) -> JSONObject:
    json_schema = _as_dict(decl.get("parametersJsonSchema"))
    if json_schema is not None:
        return cast("JSONObject", json_schema)
    parameters = _as_dict(decl.get("parameters"))
    if parameters is None:
        return {}
    return cast("JSONObject", openapi_schema_to_json_schema(parameters, losses))


def _declaration_response(decl: dict[str, Any], losses: LossLog) -> JSONObject | None:
    json_schema = _as_dict(decl.get("responseJsonSchema"))
    if json_schema is not None:
        return cast("JSONObject", json_schema)
    response = _as_dict(decl.get("response"))
    if response is None:
        return None
    return cast("JSONObject", openapi_schema_to_json_schema(response, losses))


def _convert_response_format(reader: BodyReader, losses: LossLog) -> ResponseFormat | None:
    """The response format, from `responseMimeType` with one of its schema fields
    (`responseJsonSchema`, the deprecated `_responseJsonSchema`, or the OpenAPI `responseSchema`,
    in that precedence), or else from `responseFormat.text`. The API enforces a schema it
    accepts, so the format is strict. A MIME type with no vocabulary form (`text/x.enum`) or a
    combination the vocabulary cannot carry whole stays in the extras."""
    config = ("generationConfig",)
    mime = reader.peek(*config, "responseMimeType")
    schema_keys = ("responseJsonSchema", "_responseJsonSchema", "responseSchema")
    present = [key for key in schema_keys if reader.peek(*config, key) is not None]
    if mime is None and not present:
        return _convert_text_response_format(reader)
    if mime == "text/plain" and not present:
        reader.take_leaf(*config, "responseMimeType")
        return ResponseFormatText()
    if mime != "application/json":
        if isinstance(mime, str):
            losses.note("response_mime_type", f"response MIME type {mime}")
        return None
    if not present:
        reader.take_leaf(*config, "responseMimeType")
        return ResponseFormatJsonObject()
    schema = _as_dict(reader.peek(*config, present[0]))
    if schema is None:
        return None
    reader.take_leaf(*config, "responseMimeType")
    for key in present:
        reader.take_leaf(*config, key)
    if present[0] == "responseSchema":
        schema = openapi_schema_to_json_schema(schema, losses)
    return ResponseFormatJsonSchema(schema=cast("JSONObject", schema), strict=True)


def _convert_text_response_format(reader: BodyReader) -> ResponseFormat | None:
    """`generationConfig.responseFormat.text` (`{mimeType, schema}`) as the response format."""
    path = ("generationConfig", "responseFormat", "text")
    text = _as_dict(reader.peek(*path))
    if text is None or set(text) - {"mimeType", "schema"}:
        return None
    mime, schema = text.get("mimeType"), text.get("schema")
    if mime == "TEXT_PLAIN" and schema is None:
        response_format: ResponseFormat = ResponseFormatText()
    elif mime == "APPLICATION_JSON" and schema is None:
        response_format = ResponseFormatJsonObject()
    elif mime == "APPLICATION_JSON" and isinstance(schema, dict):
        response_format = ResponseFormatJsonSchema(schema=cast("JSONObject", schema), strict=True)
    else:
        return None
    reader.take_leaf(*path)
    return response_format


# The Gemini `Schema` (OpenAPI) fields that mean the same in JSON Schema, and those whose value is a
# count (int64, which the API spells as a string).
_OPENAPI_SAME = frozenset(
    {
        "format",
        "title",
        "description",
        "enum",
        "required",
        "pattern",
        "default",
        "minimum",
        "maximum",
        "propertyOrdering",  # Gemini's own: kept as an annotation, which JSON Schema ignores
    }
)
_OPENAPI_COUNTS = frozenset(
    {"maxItems", "minItems", "minLength", "maxLength", "minProperties", "maxProperties"}
)


def openapi_schema_to_json_schema(
    schema: dict[str, Any], losses: LossLog | None = None
) -> dict[str, Any]:
    """A Gemini `Schema` (the OpenAPI subset of `responseSchema`, `parameters` and `response`) as
    JSON Schema, the vocabulary's schema language.

    The fields that differ are mapped: `type` names lowercased (`TYPE_UNSPECIFIED` left out),
    `nullable: true` as a type union with `"null"` (and `null` added to an `enum`), `example` as
    `examples`, counts given as strings as integers. `properties`, `items` and `anyOf` are
    converted recursively, and the fields that mean the same pass through. Any other field has no
    JSON Schema meaning to map to: it is left out, recorded on `losses` as `schema_keyword`.
    """
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type":
            if isinstance(value, str) and value.upper() != "TYPE_UNSPECIFIED":
                out["type"] = value.lower()
        elif key == "nullable":
            continue
        elif key == "example":
            out["examples"] = [copy.deepcopy(value)]
        elif key in _OPENAPI_COUNTS:
            out[key] = int(value) if isinstance(value, str) and value.isdigit() else value
        elif key == "properties" and isinstance(value, dict):
            out[key] = {
                name: openapi_schema_to_json_schema(_as_dict(sub) or {}, losses)
                for name, sub in cast("dict[str, Any]", value).items()
            }
        elif key == "items" and isinstance(value, dict):
            out[key] = openapi_schema_to_json_schema(cast("dict[str, Any]", value), losses)
        elif key == "anyOf" and isinstance(value, list):
            out[key] = [
                openapi_schema_to_json_schema(_as_dict(sub) or {}, losses)
                for sub in cast("list[Any]", value)
            ]
        elif key in _OPENAPI_SAME:
            out[key] = copy.deepcopy(value)
        elif losses is not None:
            losses.count("schema_keyword", f"'{key}' schema field(s) with no JSON Schema form")
    if schema.get("nullable") is True:
        if isinstance(out.get("type"), str):
            out["type"] = [out["type"], "null"]
        elif "anyOf" in out:
            out["anyOf"] = [*out["anyOf"], {"type": "null"}]
        if isinstance(out.get("enum"), list) and None not in out["enum"]:
            out["enum"] = [*out["enum"], None]
    return out


def _convert_tool_config(
    reader: BodyReader, tools: list[ToolParam], losses: LossLog
) -> tuple[ToolChoice | None, list[ToolParam]]:
    """`toolConfig.functionCallingConfig` as the tool choice, and the tools the model may call
    under it. The rest of `toolConfig` (retrieval settings, server-side tool invocations) is
    left for the extras.

    `allowedFunctionNames` restricts the calls to the functions it names: the vocabulary's
    choices range over all of the request's tools, so the others are left out of the request
    (recorded when there are any), and `ANY` restricted to one function is that tool forced.
    `VALIDATED` (calls held to their declared schema, or text) is the model's choice over tools
    that are all `strict`: per-tool strictness is how the other dialects say it.
    """
    path = ("toolConfig", "functionCallingConfig")
    mode = reader.peek(*path, "mode")
    if mode is not None:
        reader.take_leaf(*path, "mode")
    raw_names = reader.peek(*path, "allowedFunctionNames")
    names: tuple[str, ...] | None = None
    if isinstance(raw_names, list):
        reader.take_leaf(*path, "allowedFunctionNames")
        names = _str_list(raw_names)
    if mode == "NONE":
        return ToolChoiceNone(), tools
    if mode == "VALIDATED":
        tools = [replace(tool, strict=True) for tool in tools]
    if names is not None and mode in ("ANY", "VALIDATED", "AUTO", None):
        if mode == "ANY" and len(names) == 1:
            return ToolChoiceTool(name=names[0]), tools
        kept = [tool for tool in tools if tool.name in names]
        if len(kept) < len(tools):
            losses.note(
                "allowed_tools",
                f"{len(tools) - len(kept)} declared tool(s) outside allowedFunctionNames left out",
            )
        tools = kept
    if mode == "ANY":
        return ToolChoiceRequired(), tools
    if mode in ("AUTO", "VALIDATED"):
        return ToolChoiceAuto(), tools
    return None, tools


# --- model content: vocabulary blocks -> parts -------------------------------------------------

_warned_foreign_reasoning: set[str] = set()


def model_parts(
    blocks: Sequence[Content], names: ToolNames | None = None, *, for_harness: bool = False
) -> list[dict[str, Any]]:
    """An assistant turn's blocks as a model content's parts, in order: the inverse of
    `model_blocks`. Reasoning is a thought part, and its Gemini signature goes back on the part
    it rode on (the thought part, or the next part; an empty text part when nothing follows).
    A call keeps its id unless the codec made it up (`is_synthetic_call_id`).

    Reasoning Gemini has no form for (another provider's state, literal or redacted reasoning)
    is where the two sides differ. Towards the Gemini API (`for_harness` False) adaptation has
    removed it (`adapt_gemini_request`), so meeting it is a `CodecError`; a summary without state
    goes as an unsigned thought part. Towards a harness, a turn whose reasoning is not in Gemini's
    own shape carries it in bundles (`_needs_bundle`, `_bundled_parts`).
    """
    names = names if names is not None else ToolNames()
    if for_harness and _needs_bundle(blocks):
        return _bundled_parts(blocks, names)
    parts: list[dict[str, Any]] = []
    pending: str | None = None  # a signature for the next part

    def attach(part: dict[str, Any]) -> dict[str, Any]:
        nonlocal pending
        if pending is not None:
            part["thoughtSignature"] = pending
            pending = None
        return part

    def signed_thought(text: str, signature: str) -> None:
        if pending is not None:
            # A signature for the next part, and the next part brings its own: the first keeps a
            # part of its own rather than being overwritten.
            parts.append(attach({"text": ""}))
        parts.append({"text": text, "thought": True, "thoughtSignature": signature})

    for block in blocks:
        if isinstance(block, ContentReasoning):
            gemini = own_reasoning_state(block.reasoning_state, GeminiThoughtSignature)
            if gemini is not None:
                if gemini.on == "thought":
                    signed_thought(block.text, gemini.signature)
                    continue
                if block.text:
                    parts.append(attach({"text": block.text, "thought": True}))
                pending = gemini.signature
                continue
            if block.reasoning_state is not None:
                if not for_harness:
                    raise unadapted("reasoning with another provider's state")
                _warn_foreign_reasoning(block.reasoning_state.type)
                continue
            if block.form == "summary":
                if block.text:
                    parts.append(attach({"text": block.text, "thought": True}))
            elif not for_harness:
                raise unadapted(f"a reasoning {block.form} without state")
        elif isinstance(block, ContentText):
            if block.text:
                parts.append(attach({"text": block.text}))
        elif isinstance(block, ContentToolUse):
            parts.append(attach(_call_part(block, names)))
    if pending is not None:
        parts.append({"text": "", "thoughtSignature": pending})
    return parts


def _call_part(block: ContentToolUse, names: ToolNames) -> dict[str, Any]:
    call: dict[str, Any] = {
        "name": names.wire_name(block.name, block.namespace),
        "args": copy.deepcopy(dict(block.input)),
    }
    if block.id and not is_synthetic_call_id(block.id):
        call["id"] = block.id
    return {"functionCall": call}


def _display_parts(blocks: Sequence[Content], names: ToolNames) -> list[dict[str, Any]]:
    """Blocks as unsigned parts: reasoning text as thought parts, for a harness to show."""
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, ContentReasoning):
            if block.text:
                parts.append({"text": block.text, "thought": True})
        elif isinstance(block, ContentText):
            if block.text:
                parts.append({"text": block.text})
        elif isinstance(block, ContentToolUse):
            parts.append(_call_part(block, names))
    return parts


def _bundled_parts(blocks: Sequence[Content], names: ToolNames) -> list[dict[str, Any]]:
    """A turn's blocks as parts whose reasoning travels in bundles, its text also sent as thought
    parts for display. Reasoning before text is bundled on the first part of that text, where a
    harness that merges text keeps a signature (Gemini CLI keeps the first part's). Every other
    reasoning, before the first function call or anywhere after it, is bundled on that call, where
    Gemini puts a response's signature, or with no call on a trailing empty text part."""
    entries = iter(_bundle(blocks))
    parts: list[dict[str, Any]] = []
    pending: list[BundledReasoning] = []
    first_call: dict[str, Any] | None = None
    for block in blocks:
        if isinstance(block, ContentReasoning):
            if block.text:
                parts.append({"text": block.text, "thought": True})
            pending.append(next(entries))
        elif isinstance(block, ContentText):
            if not block.text:
                continue
            part: dict[str, Any] = {"text": block.text}
            if pending and first_call is None:
                part["thoughtSignature"], pending = envelope_bytes(pending), []
            parts.append(part)
        elif isinstance(block, ContentToolUse):
            parts.append(_call_part(block, names))
            first_call = first_call or parts[-1]
    if pending:
        carrier = first_call
        if carrier is None:
            carrier = {"text": ""}
            parts.append(carrier)
        carrier["thoughtSignature"] = envelope_bytes(pending)
    return parts


def _warn_foreign_reasoning(state_type: str) -> None:
    if state_type in _warned_foreign_reasoning:
        return
    _warned_foreign_reasoning.add(state_type)
    logger.warning(
        "dropping reasoning state of type %s: the Gemini API verifies the thought signatures it "
        "issued",
        state_type,
    )


# --- response: subject-model stream -> Gemini -----------------------------------------------------

# FailureCategory -> the dialect's (HTTP status, google.rpc status string).
_FAILURE_STATUSES: dict[FailureCategory, tuple[int, str]] = {
    "context_length": (400, "INVALID_ARGUMENT"),
    "content_filter": (400, "INVALID_ARGUMENT"),
    "rate_limited": (429, "RESOURCE_EXHAUSTED"),
    "overloaded": (503, "UNAVAILABLE"),
    "authentication": (401, "UNAUTHENTICATED"),
    "invalid_request": (400, "INVALID_ARGUMENT"),
    "server_error": (500, "INTERNAL"),
    "adaptation_refused": (400, "INVALID_ARGUMENT"),
}

# Gemini finishReason (and blockReason) values that stop for safety, decoded as a `StopRefusal`,
# and those that withhold output for what it contains, decoded as a `StopContentFilter`. A stop
# reason carrying one of either passes through verbatim; another provider's refusal becomes
# SAFETY, and its content filter OTHER.
_GEMINI_REFUSAL_REASONS = frozenset({"SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY"})
_GEMINI_FILTER_REASONS = frozenset({"BLOCKLIST", "SPII", "RECITATION"})
# The others the API reports, besides STOP and MAX_TOKENS: a StopOther carrying one passes
# through, so a harness sees the reason the provider gave.
_GEMINI_OTHER_REASONS = frozenset(
    {
        "OTHER",
        "LANGUAGE",
        "MALFORMED_FUNCTION_CALL",
        "UNEXPECTED_TOOL_CALL",
        "TOO_MANY_TOOL_CALLS",
        "MISSING_THOUGHT_SIGNATURE",
        "MALFORMED_RESPONSE",
        "IMAGE_PROHIBITED_CONTENT",
        "IMAGE_RECITATION",
        "IMAGE_OTHER",
        "NO_IMAGE",
        "ESCALATION",
        "PUP_LIMITED_DISABLED",
        "CONTINUATION",
        "FINISH_REASON_UNSPECIFIED",
    }
)


@dataclass(frozen=True)
class _RenderedStop:
    finish_reason: str
    rendering: StopRendering = StopRendering()


def _render_stop(stop: StopReason) -> _RenderedStop:
    """An assembled stop reason as a Gemini `finishReason`, with what that says other than as
    given. One mapping serves the encoders and `render_stop`.

    Gemini reports function-calling turns, and stop sequences, as STOP (there is no tool-use
    finish reason). Its safety stops mean the response was blocked or cut off: a refusal that is
    the model's whole answer, or one the provider did not say which of, has no form of its own.
    """
    if isinstance(stop, StopMaxTokens):
        return _RenderedStop("MAX_TOKENS")
    if isinstance(stop, StopRefusal | StopContentFilter):
        reason = stop.provider_reason.upper()
        if reason in _GEMINI_REFUSAL_REASONS | _GEMINI_FILTER_REASONS:
            finish = reason
        else:
            finish = "SAFETY" if isinstance(stop, StopRefusal) else "OTHER"
        if isinstance(stop, StopRefusal) and stop.complete is True:
            loss = ResponseLoss(
                "refusal_as_safety_stop",
                f"the model's refusal ({stop.provider_reason}) sent as a {finish} finish",
            )
            return _RenderedStop(finish, StopRendering(losses=(loss,)))
        if isinstance(stop, StopRefusal) and stop.complete is None:
            loss = ResponseLoss(
                "refusal_completeness",
                f"a refusal ({stop.provider_reason}) the provider did not say was complete, "
                f"sent as a {finish} finish",
            )
            return _RenderedStop(finish, StopRendering(losses=(loss,)))
        return _RenderedStop(finish)
    if isinstance(stop, StopOther):
        if stop.provider_reason in _GEMINI_OTHER_REASONS:
            return _RenderedStop(stop.provider_reason)
        # Another provider's stop with no Gemini form: OTHER keeps it from reading as a finished
        # answer, without the provider's name for it.
        loss = ResponseLoss(
            "stop_reason", f"a stop with no Gemini form ({stop.provider_reason}) sent as OTHER"
        )
        return _RenderedStop("OTHER", StopRendering(losses=(loss,)))
    return _RenderedStop("STOP")


def sse_event(data: object) -> bytes:
    """Encode one Gemini SSE frame: a bare `data:` line — no `event:` line, no `[DONE]` sentinel."""
    payload = json.dumps(data, separators=(",", ":"))
    return f"data: {payload}\n\n".encode()


def _usage_metadata(usage: Usage) -> dict[str, int]:
    """The Gemini `usageMetadata` object.

    `promptTokenCount` is the total including cached tokens (`Usage.input_tokens` is non-cached);
    `candidatesTokenCount` excludes thoughts, which ride `thoughtsTokenCount`.
    """
    prompt = (
        usage.input_tokens
        + (usage.cache_read_input_tokens or 0)
        + (usage.cache_write_input_tokens or 0)
    )
    reasoning = usage.reasoning_output_tokens or 0
    out = {
        "promptTokenCount": prompt,
        "candidatesTokenCount": max(usage.output_tokens - reasoning, 0),
        "totalTokenCount": prompt + usage.output_tokens,
    }
    if usage.cache_read_input_tokens is not None:
        out["cachedContentTokenCount"] = usage.cache_read_input_tokens
    if reasoning:
        out["thoughtsTokenCount"] = reasoning
    return out


def _candidate_frame(model: str, parts: list[dict[str, Any]]) -> bytes:
    """One streamed `GenerateContentResponse` frame before the last (`_final_response`)."""
    candidate = {"content": {"parts": parts, "role": "model"}, "index": 0}
    return sse_event({"candidates": [candidate], "modelVersion": model})


# What a candidate says about a filtered response beside its finish reason.
_FILTER_FIELDS = ("finishMessage", "safetyRatings")


def _final_response(
    model: str, parts: list[dict[str, Any]], response: ModelResponse
) -> dict[str, object]:
    """The response's closing `GenerateContentResponse`: the candidate with its finish reason
    and, for a filtered one, what the filter said; or, for a blocked prompt, the prompt feedback
    and no candidate. Filter detail is rendered when it is Gemini's own (another provider's has
    no Gemini form)."""
    stop = response.stop_reason
    detail = (
        dict(stop.detail)
        if isinstance(stop, StopRefusal | StopContentFilter)
        and stop.detail is not None
        and stop.provider_reason.upper()
        in _GEMINI_BLOCK_REASONS | _GEMINI_REFUSAL_REASONS | _GEMINI_FILTER_REASONS
        else {}
    )
    out: dict[str, object] = {}
    feedback = detail.get("promptFeedback")
    if isinstance(feedback, dict) and not parts:
        out["promptFeedback"] = feedback
    else:
        candidate: dict[str, object] = {
            "content": {"parts": parts, "role": "model"},
            "finishReason": _render_stop(stop).finish_reason,
            "index": 0,
        }
        candidate.update({key: detail[key] for key in _FILTER_FIELDS if key in detail})
        out["candidates"] = [candidate]
    out["usageMetadata"] = _usage_metadata(response.usage)
    out["modelVersion"] = model
    return out


# `promptFeedback.blockReason` values: why a prompt got no candidate at all.
_GEMINI_BLOCK_REASONS = frozenset(
    {"SAFETY", "OTHER", "BLOCKLIST", "PROHIBITED_CONTENT", "IMAGE_SAFETY"}
)


def _carries_next_signature(block: Content | None) -> bool:
    if not isinstance(block, ContentReasoning):
        return False
    gemini = own_reasoning_state(block.reasoning_state, GeminiThoughtSignature)
    return gemini is not None and gemini.on == "next"


async def synthesize_gemini_sse(
    model: str, items: AsyncIterator[SubjectStreamItem], names: ToolNames | None = None
) -> AsyncIterator[bytes]:
    """Replay a subject-model stream as the `streamGenerateContent?alt=sse` frame sequence.

    Until the first tool call, text and reasoning text stream as they arrive, reasoning as
    thought parts. A Gemini signature goes out when it arrives: on an empty thought part of its
    own when it rode the thought, or on the next text part. Once the reasoning streamed so far is
    not in Gemini's own shape (`_needs_bundle`), the response's reasoning travels in bundles,
    placed as `_bundled_parts` places them: reasoning before text on the text's first part, and
    everything else on the first function call or a trailing empty text part. The decoder reads
    a response with a bundle from its bundles alone, so its bundles together hold every
    reasoning block of the response, including any whose Gemini signature already went out
    raw, and no raw signature follows its first bundle. The first call
    ends the live part of the stream: Gemini has no partial-args form, so calls are rendered
    whole from the terminal response, and what follows a call (more calls, text, reasoning) rides
    the final frame after it, in the response's order, with the finish reason and
    `usageMetadata`. A terminal `ModelCallFailure` (reached after deltas already streamed) is
    surfaced as a final `{"error": ...}` data frame rather than an HTTP status (the 200 is
    already committed).
    """
    names = names if names is not None else ToolNames()
    result: ModelResponse | ModelCallFailure | None = None
    pending: str | None = None  # a streamed signature for the next part
    live = True  # before the first tool call
    segment: list[ContentReasoning] = []  # reasoning streamed since the last text
    streamed: list[BundledReasoning] = []  # reasoning before the text streamed so far
    bundled = 0  # how many of `streamed` have gone out in a bundle
    group, last_was_text = -1, False  # the content group streaming (`_content_groups`)

    async for item in items:
        if isinstance(item, (ModelResponse, ModelCallFailure)):
            result = item
            break
        if not live:
            continue
        parts: list[dict[str, Any]] = []
        if isinstance(item, StreamReasoningStart):
            segment.append(ContentReasoning(text="", form=item.form))
        elif isinstance(item, StreamReasoningDelta) and item.text:
            if not segment:
                segment.append(ContentReasoning(text="", form="full"))
            segment[-1] = replace(segment[-1], text=segment[-1].text + item.text)
            parts = [{"text": item.text, "thought": True}]
        elif isinstance(item, StreamReasoningState):
            if not segment:
                segment.append(ContentReasoning(text="", form="summary"))
            segment[-1] = replace(segment[-1], reasoning_state=item.state)
            # Once a bundle has gone out, the response's later bundles carry its reasoning: a
            # raw signature among them would split them (`read_bundles` skips only leading bytes).
            gemini = None if bundled else own_reasoning_state(item.state, GeminiThoughtSignature)
            if gemini is not None and gemini.on == "next":
                pending = gemini.signature
            elif gemini is not None:
                parts = [{"text": "", "thought": True, "thoughtSignature": gemini.signature}]
        elif isinstance(item, StreamTextDelta) and item.text:
            at = group if last_was_text else group + 1
            part: dict[str, Any] = {"text": item.text}
            if pending is not None:
                part["thoughtSignature"], pending = pending, None
            streamed += [BundledReasoning(at, b.form, b.text, b.reasoning_state) for b in segment]
            if segment and (bundled or _needs_bundle([_reasoning_of(b) for b in streamed])):
                part["thoughtSignature"] = envelope_bytes(streamed[bundled:])
                bundled = len(streamed)
            segment = []
            group, last_was_text = at, True
            parts.append(part)
        elif isinstance(item, StreamToolUseStart):
            live = False
        if parts:
            yield _candidate_frame(model, parts)
    if not isinstance(result, ModelResponse):
        failure = result or ModelCallFailure(
            category="server_error",
            description="subject-model stream ended without a result",
            provider_http_status=None,
        )
        yield sse_event({"error": _failure_error_object(failure)})
        return
    blocks = [
        block
        for message in result.output
        if isinstance(message, AssistantMessage)
        for block in message.content
    ]
    first_call = next(
        (index for index, block in enumerate(blocks) if isinstance(block, ContentToolUse)), None
    )
    if bundled or _needs_bundle(blocks):
        rest = blocks[first_call:] if first_call is not None else []
        parts = _display_parts(rest, names)
        if unbundled := _bundle(blocks)[bundled:]:
            carrier = next((part for part in parts if "functionCall" in part), None)
            if carrier is None:
                carrier = {"text": ""}
                parts.append(carrier)
            carrier["thoughtSignature"] = envelope_bytes(unbundled)
        yield sse_event(_final_response(model, parts, result))
        return
    rest: list[Content] = []
    if first_call is not None:
        begin = first_call
        if begin > 0 and _carries_next_signature(blocks[begin - 1]):
            begin -= 1  # the call's own signature, rendered with it
            pending = None
        rest = blocks[begin:]
    parts = model_parts(rest, names, for_harness=True)
    if pending is not None:
        # A signature streamed for a next part that never streamed: on the first part of the
        # rest, or ending the response on an empty text part.
        if parts and "thoughtSignature" not in parts[0]:
            parts[0]["thoughtSignature"] = pending
        else:
            parts.append({"text": "", "thoughtSignature": pending})
    yield sse_event(_final_response(model, parts, result))


def response_to_gemini_response(
    model: str, response: ModelResponse, names: ToolNames | None = None
) -> dict[str, object]:
    """Render an assembled response as a non-streaming `GenerateContentResponse` body: its
    blocks as parts, in order (`model_parts`), and how it ended (`_final_response`)."""
    blocks = [
        block
        for message in response.output
        if isinstance(message, AssistantMessage)
        for block in message.content
    ]
    return _final_response(model, model_parts(blocks, names, for_harness=True), response)


# --- errors ---------------------------------------------------------------------------------------

# The internal error vocabulary (errors a server synthesizes itself: unknown endpoint, bad JSON,
# ...) mapped to google.rpc status strings. Types with no entry fall back on the HTTP status code
# family.
_GOOGLE_STATUSES = {
    "rate_limit_error": "RESOURCE_EXHAUSTED",
    "overloaded_error": "UNAVAILABLE",
    "authentication_error": "UNAUTHENTICATED",
    "permission_error": "PERMISSION_DENIED",
    "invalid_request_error": "INVALID_ARGUMENT",
    "not_found_error": "NOT_FOUND",
    "not_implemented": "UNIMPLEMENTED",
    "api_error": "INTERNAL",
}

_STATUS_FALLBACKS = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    429: "RESOURCE_EXHAUSTED",
    501: "UNIMPLEMENTED",
    503: "UNAVAILABLE",
}


def _google_status(status: int, anthropic_type: str) -> str:
    mapped = _GOOGLE_STATUSES.get(anthropic_type)
    if mapped is not None:
        return mapped
    return _STATUS_FALLBACKS.get(status, "INTERNAL")


def _failure_error_object(failure: ModelCallFailure) -> dict[str, object]:
    status, google_status = _FAILURE_STATUSES[failure.category]
    return {"code": status, "message": failure_message(failure), "status": google_status}


def gemini_failure_body(failure: ModelCallFailure) -> tuple[int, bytes]:
    """A request-time failure as the dialect's (HTTP status, error body)."""
    status, _ = _FAILURE_STATUSES[failure.category]
    return status, json.dumps({"error": _failure_error_object(failure)}).encode("utf-8")


def gemini_error_body(status: int, anthropic_type: str, message: str) -> bytes:
    """Encode a Google-API-shaped error body (`{"error": {code, message, status}}`).

    Unlike the other dialects', Gemini error bodies embed the numeric HTTP code, so this hook
    takes the status too.
    """
    return json.dumps(
        {
            "error": {
                "code": status,
                "message": message,
                "status": _google_status(status, anthropic_type),
            }
        }
    ).encode("utf-8")


# --- model side: ModelRequest -> generateContent request ----------------------------------------
#
# Rendering notes. The encoder renders a request as it is; what the dialect has no form for is
# changed beforehand by `adapt_gemini_request`, which records each change, and meeting it here is a
# `CodecError` (`_common.unadapted`).
#
# - The model and the method are the path (`/v1beta/models/{model}:streamGenerateContent` with
#   `alt=sse`, or `:generateContent`); the body has no `model`.
# - Leading `SystemMessage`s are `systemInstruction`, a part per block. The dialect has no system
#   role inside `contents`, so adaptation sends a later one as a user message: its instructions
#   reach the model as the user's words.
# - Consecutive messages of one side are one content: the model's turn (`role: model`), or the
#   user's (`role: user`), where tool results are `functionResponse` parts named after the call
#   they answer, `{"output": text}` or, for an error, `{"error": text}`. A call keeps its id unless
#   the codec made it up, so a history pairs by name and order as it came.
# - Parallel calls a dialect re-paired with their results (Responses does) go back together: the
#   API signs the first call of each response, refuses a signed call interleaved with results,
#   and so a calls-only turn with no signature after a signed one, with nothing between but their
#   results, joins it (`_is_parallel_sibling`).
# - Reasoning goes back as `model_parts` renders it: summaries as thought parts, Gemini
#   signatures on the parts they rode on. Adaptation removes any other reasoning state, and
#   reasoning without state other than a summary, which have no Gemini form.
# - Tools are `functionDeclarations` with `parametersJsonSchema`, named as a dialect without
#   namespaces names them (`ToolNames`); the tool choice is `toolConfig.functionCallingConfig`
#   (`AUTO`, `ANY`, `NONE`, `ANY` restricted to one name for a forced tool).
# - `SamplingParams` map to `generationConfig` (`maxOutputTokens`, `temperature`, `topP`,
#   `stopSequences`); `ReasoningParams` to `thinkingConfig`: off is `thinkingBudget: 0`, a budget
#   a budget, an effort a `thinkingLevel` (adaptation sends `xhigh` and `max` as `high`), and
#   `summary` `includeThoughts`. Which of these a model takes is the model's (Gemini 3 has no off
#   switch; a budget and a level together are refused): the codec renders what was asked.
# - `SamplingParams.speed` and cache breakpoints have no generateContent form; adaptation removes
#   them.
# - Extras of this dialect are merged in last (`apply_dialect_extras`).


def adapt_gemini_request(
    model: str, request: ModelRequest, accepts: ReasoningStateFilter
) -> AdaptedRequest:
    """`request` changed so the Gemini API can carry it, one loss per change.

    The dialect has no system role inside the conversation, so a later system message is sent as
    the user's words. It takes back reasoning Gemini signed for `model` (what `accepts`, the
    codec's `accepts_reasoning_state`, lets through) and summaries without state, so other
    reasoning state, and the model's own reasoning text or a redaction without state, are left
    out. Effort `xhigh` and `max` are
    sent as `high`, the highest level there is. It has no speed tier and no cache breakpoints.
    """
    adaptation = AdaptedRequestBuilder(request)
    drop_foreign_extras(adaptation, "gemini_generate_content")
    drop_foreign_reasoning(adaptation, accepts)
    later_system_messages_as_user(adaptation)
    drop_assistant_blocks(
        adaptation,
        lambda block: (
            isinstance(block, ContentReasoning)
            and block.reasoning_state is None
            and block.form == "full"
        ),
        "literal_reasoning",
        "reasoning text block(s) with no Gemini signature",
    )
    drop_assistant_blocks(
        adaptation,
        lambda block: (
            isinstance(block, ContentReasoning)
            and block.reasoning_state is None
            and block.form == "redacted"
        ),
        "redacted_reasoning",
        "redacted reasoning block(s) with no Gemini signature",
    )
    reasoning = adaptation.request.sampling.reasoning
    if reasoning is not None and reasoning.enabled and reasoning.effort in ("xhigh", "max"):
        replace_reasoning(
            adaptation,
            replace(reasoning, effort="high"),
            "reasoning_effort",
            f"effort {reasoning.effort} sent as high, the highest level the API offers",
        )
    note_flattened_tools(adaptation)
    drop_speed(adaptation)
    drop_breakpoints(adaptation, lambda _value, _container: True, "(the API has no breakpoints)")
    enforce_response_format(adaptation)
    move_rejected_keywords(adaptation, gemini_rejects)
    note_unenforced_response_keywords(adaptation, "gemini")
    resolve_automatic_strictness(adaptation)
    fit_strict_tools_to_gemini(adaptation)
    _drop_unmatched_output_schemas(adaptation)
    return adaptation.result()


def _drop_unmatched_output_schemas(adaptation: AdaptedRequestBuilder) -> None:
    """Leave out the output schema of a function whose results in the context are not all the
    JSON object a schema describes (text from a harness of another dialect, say): the encoder
    sends a declared function's result as that object, and wraps any other result under
    `output`, which the schema would not match. Nothing is inferred from a result's text for a
    function without a schema."""
    request = adaptation.request
    declared = {
        (tool.namespace, tool.name) for tool in request.tools if tool.output_schema is not None
    }
    if not declared:
        return
    called = {
        block.id: (block.namespace, block.name)
        for message in request.context
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, ContentToolUse)
    }
    unmatched = {
        called[message.tool_call_id]
        for message in request.context
        if isinstance(message, ToolMessage)
        and not message.is_error
        and called.get(message.tool_call_id) in declared
        and structured_result("".join(block.text for block in message.content)) is None
    }
    if unmatched:
        drop_output_schemas(
            adaptation,
            "tool_result_shape",
            "a result is not the object its output schema describes, so the schema is left out",
            only=frozenset(unmatched),
        )


def encode_gemini_request(model: str, request: ModelRequest, *, streaming: bool) -> EncodedRequest:
    """Render a request as a generateContent request for `model`."""
    if request.sampling.speed is not None:
        raise unadapted("a speed tier")
    if any(breakpoint_of(value) is not None for value in _markable(request)):
        raise unadapted("a cache breakpoint")
    names = ToolNames(request.tools, flattened=True)
    body: dict[str, Any] = {}
    system, contents = _render_contents(
        request.context,
        names,
        frozenset(
            (tool.namespace, tool.name) for tool in request.tools if tool.output_schema is not None
        ),
    )
    body["contents"] = contents
    if system:
        body["systemInstruction"] = {"parts": system}
    if request.tools:
        body["tools"] = [
            {"functionDeclarations": [_declaration(tool, names) for tool in request.tools]}
        ]
    if any(tool.strict == "auto" for tool in request.tools):
        raise unadapted("a tool's automatic strictness")
    tool_config = _tool_config(
        request.tool_choice, names, any(tool.strict is True for tool in request.tools)
    )
    if tool_config is not None:
        body["toolConfig"] = tool_config
    generation = _generation_config(request.sampling, request.response_format)
    if generation:
        body["generationConfig"] = generation
    extra_headers, extra_query = apply_dialect_extras(
        body, request.extras, "gemini_generate_content"
    )
    merge_provider_params(body, request.sampling.extra)
    method = "streamGenerateContent" if streaming else "generateContent"
    query = (("alt", "sse"),) if streaming else ()
    return EncodedRequest(
        path=f"/v1beta/models/{model}:{method}",
        body=cast("JSONObject", body),
        query=(*query, *extra_query),
        headers=extra_headers,
    )


def _markable(request: ModelRequest) -> Iterator[object]:
    """Every tool, message and block of `request` that could carry a cache breakpoint."""
    yield from request.tools
    for message in request.context:
        yield message
        yield from message.content


def _render_contents(
    context: Sequence[ChatMessage],
    names: ToolNames,
    structured: frozenset[tuple[str | None, str]] = frozenset(),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The leading system parts and the `contents` list. A result of a function in `structured`
    (one that declares an output schema, so its result text is the JSON object the schema
    describes; keyed by namespace and name) is sent as that object, the inverse of the decoder's
    whole-object reading."""
    system: list[dict[str, Any]] = []
    contents: list[dict[str, Any]] = []
    call_names: dict[str, str] = {}
    called: dict[str, tuple[str | None, str]] = {}
    leading = True
    for message in context:
        if isinstance(message, SystemMessage) and leading:
            system.extend({"text": block.text} for block in message.content)
            continue
        leading = False
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ContentToolUse):
                    call_names[block.id] = names.wire_name(block.name, block.namespace)
                    called[block.id] = (block.namespace, block.name)
            parts = model_parts(message.content, names)
            if _is_parallel_sibling(contents, parts):
                # Calls of the model response before, each re-paired with its result by a
                # dialect that does so (Responses): the API refuses a signed call interleaved
                # with results (call + signature, result, call, result), so the calls go back
                # together, and their results with them, after them.
                contents[-2]["parts"].extend(parts)
                continue
            _append(contents, "model", parts)
        elif isinstance(message, ToolMessage):
            text = "".join(block.text for block in message.content)
            whole = (
                structured_result(text)
                if called.get(message.tool_call_id) in structured and not message.is_error
                else None
            )
            if (
                whole is None
                and not message.is_error
                and called.get(message.tool_call_id) in (structured)
            ):
                raise unadapted("a result that is not the object its output schema describes")
            response: dict[str, Any] = {
                "name": call_names.get(message.tool_call_id, ""),
                "response": (
                    whole
                    if whole is not None
                    else {("error" if message.is_error else "output"): text}
                ),
            }
            if message.tool_call_id and not is_synthetic_call_id(message.tool_call_id):
                response["id"] = message.tool_call_id
            _append(contents, "user", [{"functionResponse": response}])
        else:
            if isinstance(message, SystemMessage):
                raise unadapted("a system message after the conversation began")
            _append(contents, "user", [{"text": block.text} for block in message.content])
    return system, contents


def structured_result(text: str) -> dict[str, Any] | None:
    """`text` as the JSON object a function's output schema describes, or None if it is not
    one (a result that would not match the declared schema)."""
    try:
        value = loads_strict_json(text)
    except ValueError:
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _is_parallel_sibling(contents: list[dict[str, Any]], parts: list[dict[str, Any]]) -> bool:
    """Whether a model turn's parts are calls of the response before it: calls only, none
    signed, after a model turn whose calls are signed (the API signs the first call of each
    response, not the calls beside it) and nothing since but those calls' results."""
    if not parts or not all(
        "functionCall" in part and "thoughtSignature" not in part for part in parts
    ):
        return False
    if len(contents) < 2 or contents[-1]["role"] != "user" or contents[-2]["role"] != "model":
        return False
    if not all("functionResponse" in part for part in contents[-1]["parts"]):
        return False
    return any(
        "functionCall" in part and "thoughtSignature" in part for part in contents[-2]["parts"]
    )


def _append(contents: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]) -> None:
    """Add parts to the conversation: to the last content when it is the same side's."""
    if not parts:
        return
    if contents and contents[-1]["role"] == role:
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": role, "parts": parts})


def _declaration(tool: ToolParam, names: ToolNames) -> dict[str, Any]:
    declaration: dict[str, Any] = {"name": names.wire_name(tool.name, tool.namespace)}
    if tool.description is not None:
        declaration["description"] = tool.description
    if tool.input_schema:
        declaration["parametersJsonSchema"] = copy.deepcopy(dict(tool.input_schema))
    if tool.output_schema is not None:
        declaration["responseJsonSchema"] = copy.deepcopy(dict(tool.output_schema))
    return declaration


def _tool_config(
    choice: ToolChoice | None, names: ToolNames, strict: bool
) -> dict[str, Any] | None:
    """`toolConfig.functionCallingConfig` for the choice. Gemini validates calls for the whole
    request rather than per tool, so any strict tool makes the model's choice `VALIDATED`."""
    if (choice is None or isinstance(choice, ToolChoiceAuto)) and strict:
        config: dict[str, Any] = {"mode": "VALIDATED"}
    elif choice is None:
        return None
    elif isinstance(choice, ToolChoiceAuto):
        config = {"mode": "AUTO"}
    elif isinstance(choice, ToolChoiceNone):
        config = {"mode": "NONE"}
    elif isinstance(choice, ToolChoiceRequired):
        config = {"mode": "ANY"}
    else:
        config = {"mode": "ANY", "allowedFunctionNames": [names.choice_wire_name(choice.name)]}
    return {"functionCallingConfig": config}


def _generation_config(
    sampling: SamplingParams, response_format: ResponseFormat | None = None
) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if isinstance(response_format, ResponseFormatText):
        config["responseMimeType"] = "text/plain"
    elif response_format is not None:
        config["responseMimeType"] = "application/json"
        if isinstance(response_format, ResponseFormatJsonSchema):
            # The API always enforces the schema and has no slot for a description.
            if not response_format.strict:
                raise unadapted("an advisory (non-strict) response schema")
            if response_format.description is not None:
                raise unadapted("a response format's description")
            config["responseJsonSchema"] = copy.deepcopy(dict(response_format.schema))
    if sampling.max_tokens is not None:
        config["maxOutputTokens"] = sampling.max_tokens
    if sampling.temperature is not None:
        config["temperature"] = sampling.temperature
    if sampling.top_p is not None:
        config["topP"] = sampling.top_p
    if sampling.stop_sequences:
        config["stopSequences"] = list(sampling.stop_sequences)
    thinking = _thinking_config(sampling.reasoning)
    if thinking is not None:
        config["thinkingConfig"] = thinking
    return config


def _thinking_config(reasoning: ReasoningParams | None) -> dict[str, Any] | None:
    if reasoning is None:
        return None
    config: dict[str, Any] = {}
    if not reasoning.enabled:
        config["thinkingBudget"] = 0
    elif reasoning.budget_tokens is not None:
        config["thinkingBudget"] = reasoning.budget_tokens
    if reasoning.enabled and reasoning.effort is not None:
        effort = reasoning.effort
        if effort in ("xhigh", "max"):
            raise unadapted(f"reasoning effort {effort}")
        config["thinkingLevel"] = effort.upper()
    if reasoning.summary is not None:
        config["includeThoughts"] = reasoning.summary
    return config


# --- model side: provider response -> ModelResponse ----------------------------------------------


class _IssuedCallIds(_ToolCallIds):
    """Ids for the calls of one response that arrive without one: unique across responses (a
    harness keeps its tool calls by id through the whole conversation), and the same ones for the
    streamed events and the assembled response."""

    def __init__(self) -> None:
        super().__init__()
        self._issued: list[str] = []
        self._next = 0

    def call_id(self, explicit: Any, name: str) -> str:
        if isinstance(explicit, str) and explicit:
            return explicit
        if self._next == len(self._issued):
            self._issued.append(f"{SYNTHETIC_CALL_ID_PREFIX}{uuid.uuid4().hex[:24]}")
        call_id = self._issued[self._next]
        self._next += 1
        return call_id

    def rewind(self) -> None:
        """Hand out the same ids again, from the first, for another pass over the same calls."""
        self._next = 0


def _joined_parts(parts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """A streamed response's parts as one response's: consecutive fragments of one text joined,
    the way `model_blocks(join=True)` reads them (a thought run until the part carrying its
    signature; a text run from the part carrying its signature on), and empty unsigned text
    dropped."""
    out: list[dict[str, Any]] = []
    for part in parts:
        if _is_plain_text(part) and not part.get("text") and "thoughtSignature" not in part:
            continue
        previous = out[-1] if out else None
        if previous is not None:
            if _is_thought(previous) and _is_thought(part) and "thoughtSignature" not in previous:
                merged = previous | part
                merged["text"] = str(previous.get("text", "")) + str(part.get("text", ""))
                out[-1] = merged
                continue
            if _is_plain_text(previous) and _is_plain_text(part) and "thoughtSignature" not in part:
                out[-1] = previous | {
                    "text": str(previous.get("text", "")) + str(part.get("text", ""))
                }
                continue
        out.append(dict(part))
    return out


def _is_thought(part: dict[str, Any]) -> bool:
    return part.get("thought") is True


def _is_plain_text(part: dict[str, Any]) -> bool:
    return (
        "text" in part
        and part.get("thought") is not True
        and len(set(part) - {"text", "thoughtSignature"}) == 0
    )


class _ResponseAssembler:
    """The state of one response read from the API: its parts so far, and the rest of the
    candidate and response fields, the latest frame's winning."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.parts: list[dict[str, Any]] = []
        self.ids = _IssuedCallIds()
        self.finish_reason: str | None = None
        self.block_reason: str | None = None
        self.usage: dict[str, Any] = {}
        self.response_id: str | None = None
        self.model_version: str | None = None
        self.calls = 0
        self.prompt_feedback: dict[str, Any] | None = None
        self.candidate_fields: dict[str, Any] = {}  # `finishMessage`, `safetyRatings`
        self._reasoning_open = False  # a thought's text has streamed and no signature closed it

    def add(self, frame: dict[str, Any]) -> list[StreamEvent]:
        """Take one `GenerateContentResponse` (a frame, or a whole body), returning its events."""
        response_id = frame.get("responseId")
        if isinstance(response_id, str):
            self.response_id = response_id
        version = frame.get("modelVersion")
        if isinstance(version, str):
            self.model_version = version
        usage = _as_dict(frame.get("usageMetadata"))
        if usage:
            self.usage = usage
        feedback = _as_dict(frame.get("promptFeedback"))
        if feedback is not None:
            self.prompt_feedback = feedback
            if isinstance(feedback.get("blockReason"), str):
                self.block_reason = str(feedback["blockReason"])
        events: list[StreamEvent] = []
        for candidate in _dict_list(frame.get("candidates"))[:1]:
            reason = candidate.get("finishReason")
            if isinstance(reason, str):
                self.finish_reason = reason
            for key in _FILTER_FIELDS:
                if key in candidate:
                    self.candidate_fields[key] = candidate[key]
            content = _as_dict(candidate.get("content")) or {}
            for part in _dict_list(content.get("parts")):
                self.parts.append(part)
                events.extend(self._events(part))
        return events

    def _events(self, part: dict[str, Any]) -> list[StreamEvent]:
        raw = part.get("thoughtSignature")
        signature = raw if isinstance(raw, str) and raw else None
        text = part.get("text")
        out: list[StreamEvent] = []
        if part.get("thought") is True:
            if isinstance(text, str) and text:
                if not self._reasoning_open:
                    self._reasoning_open = True
                    out.append(StreamReasoningStart(form="summary"))
                out.append(StreamReasoningDelta(text=text))
            if signature is not None:
                if not self._reasoning_open:
                    out.append(StreamReasoningStart(form="summary"))
                self._reasoning_open = False
                out.append(
                    StreamReasoningState(state=_gemini_state(self.producer, signature, "thought"))
                )
            return out
        if signature is not None:
            if not self._reasoning_open:
                out.append(StreamReasoningStart(form="summary"))
            out.append(StreamReasoningState(state=_gemini_state(self.producer, signature, "next")))
        self._reasoning_open = False
        call = _as_dict(part.get("functionCall"))
        if call is not None:
            name = str(call.get("name", ""))
            index = self.calls
            self.calls += 1
            out.append(
                StreamToolUseStart(
                    index=index, id=self.ids.call_id(call.get("id"), name), name=name
                )
            )
            out.append(
                StreamToolUseInputDelta(
                    index=index, partial_json=json.dumps(_as_dict(call.get("args")) or {})
                )
            )
        elif isinstance(text, str) and text:
            out.append(StreamTextDelta(text=text))
        return out

    @property
    def producer(self) -> str:
        """The model the signatures are for: the one requested, which the next request will name
        (the response's `modelVersion` is a dated variant of it), or the version the response
        names when the decoder was given no model."""
        return gemini_model_id(self.model or self.model_version or "")

    def response(self, *, join: bool) -> ModelResponse:
        self.ids.rewind()
        parts = _joined_parts(self.parts) if join else self.parts
        blocks = model_blocks(parts, self.producer, self.ids, LossLog(), join=join)
        return ModelResponse(
            id=self.response_id or f"gemini-{uuid.uuid4().hex[:24]}",
            output=(AssistantMessage(content=tuple(blocks)),) if blocks else (),
            stop_reason=self._stop(blocks),
            usage=_usage_from_metadata(self.usage),
        )

    def _stop(self, blocks: Sequence[Content]) -> StopReason:
        if self.block_reason is not None and not self.parts:
            # The prompt was blocked: no candidate, the feedback is the whole of what was said.
            feedback = cast("JSONObject", {"promptFeedback": self.prompt_feedback})
            if self.block_reason in _GEMINI_REFUSAL_REASONS:
                return StopRefusal(
                    provider_reason=self.block_reason, complete=False, detail=feedback
                )
            return StopContentFilter(provider_reason=self.block_reason, detail=feedback)
        detail = cast("JSONObject", dict(self.candidate_fields)) or None
        reason = self.finish_reason or "STOP"
        if reason == "STOP":
            if any(isinstance(block, ContentToolUse) for block in blocks):
                return StopToolUse()
            return StopEndTurn()
        if reason == "MAX_TOKENS":
            return StopMaxTokens()
        if reason in _GEMINI_REFUSAL_REASONS:
            return StopRefusal(provider_reason=reason, complete=False, detail=detail)
        if reason in _GEMINI_FILTER_REASONS:
            return StopContentFilter(provider_reason=reason, detail=detail)
        return StopOther(provider_reason=reason)

    def raw(self, *, join: bool) -> JSONObject:
        """The response as one `GenerateContentResponse`, for the reconstruction check."""
        out: dict[str, Any] = {"usageMetadata": self.usage}
        if self.prompt_feedback is not None:
            out["promptFeedback"] = self.prompt_feedback
        if self.parts or self.finish_reason is not None:
            candidate: dict[str, Any] = {
                "content": {
                    "parts": _joined_parts(self.parts) if join else self.parts,
                    "role": "model",
                },
                "index": 0,
                **self.candidate_fields,
            }
            if self.finish_reason is not None:
                candidate["finishReason"] = self.finish_reason
            out["candidates"] = [candidate]
        if self.model_version is not None:
            out["modelVersion"] = self.model_version
        if self.response_id is not None:
            out["responseId"] = self.response_id
        return cast("JSONObject", out)


def _usage_from_metadata(usage: dict[str, Any]) -> Usage:
    """`usageMetadata` as Usage. `promptTokenCount` includes cached tokens, which `Usage` keeps
    apart; `candidatesTokenCount` excludes thoughts, which `Usage` counts as output too."""
    prompt = _as_int(usage.get("promptTokenCount")) or 0
    cached = _as_int(usage.get("cachedContentTokenCount"))
    candidates = _as_int(usage.get("candidatesTokenCount")) or 0
    thoughts = _as_int(usage.get("thoughtsTokenCount"))
    return Usage(
        input_tokens=max(prompt - (cached or 0), 0),
        cache_read_input_tokens=cached,
        cache_write_input_tokens=None,
        output_tokens=candidates + (thoughts or 0),
        reasoning_output_tokens=thoughts,
    )


class GeminiSseDecoder:
    """Decodes a `streamGenerateContent?alt=sse` stream into stream events and the assembled
    response.

    Every frame is a whole `GenerateContentResponse` carrying the parts that arrived since the
    last; the parts are relayed as events and kept, and assembled at the end with consecutive
    text fragments joined. The frame with a `finishReason` is the last. An `{"error": ...}` frame
    ends the stream as a failure.
    """

    def __init__(self, model: str = "") -> None:
        self._frames = SseFrameParser()
        self._assembler = _ResponseAssembler(model)
        self._failure: ModelCallFailure | None = None

    def _consume(self, frames: list[tuple[str | None, str]]) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for _event, data in frames:
            if self._failure is not None:
                continue
            frame = decode_json_object(data, "Gemini stream frame")
            error = _as_dict(frame.get("error"))
            if error is not None:
                self._failure = failure_from_error_object(error)
                continue
            events.extend(self._assembler.add(frame))
        return events

    def feed(self, chunk: bytes) -> list[StreamEvent]:
        return self._consume(self._frames.feed(chunk))

    def complete(self) -> bool:
        # A blocked prompt ends the response without a candidate, so without a finish reason.
        assembler = self._assembler
        return (
            self._failure is not None
            or assembler.finish_reason is not None
            or (assembler.block_reason is not None and not assembler.parts)
        )

    def finish(self) -> ModelResponse | ModelCallFailure:
        self._consume(self._frames.finish())
        if self._failure is not None:
            return self._failure
        if self._assembler.finish_reason is None and self._assembler.block_reason is None:
            return ModelCallFailure(
                category="server_error",
                description="Gemini stream ended without a finish reason",
                provider_http_status=None,
            )
        return self._assembler.response(join=True)

    def raw_message(self) -> JSONObject | None:
        if self._failure is not None or not self.complete():
            return None
        return self._assembler.raw(join=True)


def decode_gemini_response(body: bytes, model: str = "") -> ModelResponse | ModelCallFailure:
    """A complete (non-streamed) `GenerateContentResponse` body as a ModelResponse."""
    response = decode_json_object(body, "Gemini response body")
    error = _as_dict(response.get("error"))
    if error is not None:
        return failure_from_error_object(error)
    assembler = _ResponseAssembler(model)
    assembler.add(response)
    return assembler.response(join=False)


def decode_gemini_failure(status: int, body: bytes) -> ModelCallFailure:
    """A non-success response as a failure, from its Google error object
    (`{"error": {"code", "message", "status"}}`) or, from a gateway, its text."""
    return decode_failure_body(status, body)


class GeminiGenerateContentCodec:
    """The Gemini generateContent dialect (`/v1beta/models/{model}:generateContent`), both halves.

    Args:
        model: The model the model side serves, recorded as the model of the thought signatures
            its decoders read.
    """

    dialect: Dialect = "gemini_generate_content"
    reasoning_round_trip = True

    def __init__(self, *, model: str = "") -> None:
        self._model = model

    # --- model side -----------------------------------------------------------------------------

    def adapt_request(self, model: str, request: ModelRequest) -> AdaptedRequest:
        return adapt_gemini_request(
            model, request, lambda state: self.accepts_reasoning_state(state, model)
        )

    def encode_request(
        self, model: str, request: ModelRequest, *, streaming: bool
    ) -> EncodedRequest:
        return encode_gemini_request(model, request, streaming=streaming)

    def sse_decoder(self) -> SseDecoder:
        return GeminiSseDecoder(self._model)

    def decode_response(self, body: bytes) -> ModelResponse | ModelCallFailure:
        return decode_gemini_response(body, self._model)

    def decode_failure(self, status: int, body: bytes) -> ModelCallFailure:
        return decode_gemini_failure(status, body)

    def accepts_reasoning_state(self, state: DynamicData, model: str) -> bool:
        """A Gemini thought signature, issued for `model`: whether the API accepts one model's
        signature from another is unverified, so it is not sent."""
        gemini = own_reasoning_state(state, GeminiThoughtSignature)
        return gemini is not None and gemini.model == gemini_model_id(model)

    # --- harness side ---------------------------------------------------------------------------

    def matches_path(self, target: str) -> bool:
        return parse_gemini_target(target) is not None

    def matches_headers(self, headers: Headers) -> bool:
        return has_header(headers, "x-goog-api-key")

    def matches_body(self, body: JSONObject) -> bool:
        return "contents" in body and "messages" not in body

    def route(self, target: str, body: JSONObject) -> Route:
        """`countTokens` and `embedContent` are refused so a harness depending on them fails
        loudly, and `streamGenerateContent` without `alt=sse` would need chunked JSON-array
        framing that nothing we run asks for (the google-genai SDKs always request SSE)."""
        parsed = parse_gemini_target(target)
        if parsed is None:
            # Detected as Gemini by header or body, but with no model in the path there is nothing
            # to serve it with.
            raise RoutingError(
                404, "not_found_error", f"unrecognized Gemini API endpoint: {target}"
            )
        if parsed.method in ("countTokens", "embedContent"):
            raise RoutingError(
                501, "not_implemented", f"the '{parsed.method}' endpoint is not implemented yet"
            )
        if parsed.method == "streamGenerateContent" and not parsed.alt_sse:
            raise RoutingError(
                501,
                "not_implemented",
                "streamGenerateContent without alt=sse is not supported; request SSE framing",
            )
        return Route(
            model=parsed.model,
            streaming=parsed.method == "streamGenerateContent",
            kind="completion",
        )

    def decode_request(self, target: str, headers: Headers, body: bytes) -> DecodedRequest:
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise ValueError("request body is not a JSON object")
        return gemini_request_to_canonical(target, cast("dict[str, Any]", parsed), headers)

    def provider_tools(self, body: JSONObject) -> tuple[ProviderTool, ...]:
        return provider_tools_in_request(cast("dict[str, Any]", body))

    def strip_reasoning(self, request: ModelRequest) -> ModelRequest:
        return without_reasoning(request, (("generationConfig", "thinkingConfig"),))

    def encode_sse(
        self,
        model: str,
        items: AsyncIterator[SubjectStreamItem],
        *,
        tools: tuple[ToolParam, ...] = (),
    ) -> AsyncIterator[bytes]:
        return synthesize_gemini_sse(model, items, ToolNames(tools, flattened=True))

    def keepalive_frame(self) -> bytes | None:
        # None: every Gemini frame is a `GenerateContentResponse`, and Gemini CLI's SSE reader
        # rejects an SSE comment line (it fails the turn with "Incomplete JSON segment").
        return None

    def encode_response(
        self, model: str, response: ModelResponse, *, tools: tuple[ToolParam, ...] = ()
    ) -> JSONObject:
        return cast(
            "JSONObject",
            response_to_gemini_response(model, response, ToolNames(tools, flattened=True)),
        )

    def encode_failure(self, failure: ModelCallFailure) -> tuple[int, bytes]:
        return gemini_failure_body(failure)

    def render_stop(self, stop: StopReason, *, streaming: bool) -> StopRendering:
        return _render_stop(stop).rendering

    def encode_error(self, status: int, error_type: str, message: str) -> bytes:
        return gemini_error_body(status, error_type, message)
