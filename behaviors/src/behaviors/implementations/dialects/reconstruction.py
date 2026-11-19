"""The reconstruction check: does a dialect's codec reproduce a request or response when it
decodes it and re-encodes the result in the same dialect?

`compare_request` compares a request body, plus the extras headers and query it should carry
(`reference_extras`), with its re-encoding; `compare_response` does the same for a response
body, on content, stop reason and usage. Each returns the leaf `Difference`s and the names of
the normalizations it applied. A server passing a harness's calls through unchanged can run the
check on each to catch what its codec would lose; the dialect coverage audit runs it over its
probe corpus.

Both comparisons first normalize what the dialect treats as equal spellings and skip fields
listed with a reason (`_request_spellings`, `_response_projection`, `DECLARED_DEFAULTS`). The rule
for those lists: they start narrow and grow only from what `halt`-mode runs against real providers
report, never from a guess about what a provider might send, and every entry says why the field
is not identity. Tool-call ids and anything a continuation must echo back (signatures, encrypted
reasoning) are always compared.
"""

import copy
import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from behaviors.implementations.dialects._common import leftover_headers, leftover_query
from behaviors.implementations.dialects.gemini_generate_content import openapi_schema_to_json_schema
from behaviors.implementations.dialects.openai_responses import SYNTHETIC_REASONING_ID_PREFIX
from behaviors.types.dialect import Dialect, Headers, QueryParams
from behaviors.types.json import JSONObject, JSONValue
from behaviors.types.namespace import behaviors_type
from fxtr.entity_defns.dataclass_bases import TaggedDataclass


@dataclass(frozen=True)
class Difference(TaggedDataclass, fxtr_type=behaviors_type("Difference.v1")):
    """One place where a re-encoded request or response differs from the harness's or the
    provider's.

    Attributes:
        path: JSON pointer to the differing leaf, e.g. `/output_config/effort`.
        kind: Whether the leaf is missing from the translated side, extra on it, or present on
            both with different values.
        harness: The value on the harness's (or provider's) side, or None when missing.
        translated: The value on the translated side, or None when missing.
    """

    path: str
    kind: Literal["missing_in_translated", "extra_in_translated", "value_differs"]
    harness: JSONValue | None
    translated: JSONValue | None


# Keys a codec fills in when the request left them absent: they are ignored
# on the encoded side whenever the raw request omitted them.
DECLARED_DEFAULTS: dict[Dialect, tuple[tuple[str, ...], ...]] = {
    "anthropic_messages": (("max_tokens",), ("thinking", "display")),
    "openai_responses": (("store",), ("reasoning", "summary")),
    "openai_chat_completions": (),
    "gemini_generate_content": (),
}


# What each built-in codec's decoder excludes from the extras beyond the shared classification
# (`leftover_headers` / `leftover_query`): its contract-selection header and the query names it
# consumes. The reference side of the header/query check is derived from the original request
# with these, independently of the codec's own decoding, so a decoder that drops more than it
# should is caught. A codec that consumes more than listed here reports false differences until
# the table learns it; a mistake in the shared classification itself is invisible to the check,
# since both sides would apply it.
CONTRACT_HEADERS: dict[Dialect, tuple[str, ...]] = {
    "anthropic_messages": ("anthropic-version",),
    "openai_responses": (),
    "openai_chat_completions": (),
    "gemini_generate_content": (),
}
CONSUMED_QUERY: dict[Dialect, tuple[str, ...]] = {
    "anthropic_messages": (),
    "openai_responses": (),
    "openai_chat_completions": (),
    "gemini_generate_content": ("alt",),
}


def reference_extras(
    dialect: Dialect, headers: Headers, target: str
) -> tuple[Headers, QueryParams]:
    """The extras headers and query a request in `dialect` should carry, from the request itself
    (see `CONTRACT_HEADERS` / `CONSUMED_QUERY`)."""
    return (
        leftover_headers(headers, contract=CONTRACT_HEADERS[dialect]),
        leftover_query(target, consumed=CONSUMED_QUERY[dialect]),
    )


def compare_request(
    dialect: Dialect,
    raw: JSONObject,
    encoded: JSONObject,
    *,
    raw_headers: Headers = (),
    raw_query: QueryParams = (),
    encoded_headers: Headers = (),
    encoded_query: QueryParams = (),
) -> tuple[tuple[Difference, ...], tuple[str, ...]]:
    """The reconstruction check on the request side: the harness's body against the same
    dialect's re-encoding of its decoding, and the extras headers and query the request should
    carry (`reference_extras`, derived from the request, not from the decoder) against what
    the encoder sends.

    Normalized away: `model` (rewritten by design), `stream` (the subject call always streams),
    the dialect's equivalent spellings of content, JSON whitespace in re-encoded tool arguments,
    synthesized reasoning-item ids (compared by presence, not value), and the codec's declared
    defaults where the raw request omitted them. The returned ignore list names each
    normalization that changed either side.
    Header names are compared case-insensitively; repeated names as their list of values.
    Differences in headers and query are reported under `/headers/<name>` and `/query/<name>`.
    """
    left, ignored = normalized_request(dialect, raw)
    right, fired_encoded = normalized_request(dialect, encoded)
    ignored.extend(fired_encoded)
    for path in DECLARED_DEFAULTS.get(dialect, ()):
        if not _has(left, path) and _has(right, path):
            _remove(right, path)
            ignored.append(f"declared default /{'/'.join(path)}")
    differences = _diff(left, right, "")
    differences.extend(
        _diff(_multi(raw_headers, fold=True), _multi(encoded_headers, fold=True), "/headers")
    )
    # The query names the codec consumes are its own framing (Gemini's `alt=sse`, which the
    # encoder sends for a streamed call), not extras, on either side.
    consumed = CONSUMED_QUERY.get(dialect, ())
    sent = tuple((name, value) for name, value in encoded_query if name not in consumed)
    differences.extend(_diff(_multi(raw_query), _multi(sent), "/query"))
    return tuple(differences), tuple(dict.fromkeys(ignored))


def normalized_request(dialect: Dialect, body: JSONObject) -> tuple[dict[str, Any], list[str]]:
    """A request body as `compare_request` compares it, before declared defaults: without
    `model` and `stream`, the dialect's equivalent spellings in canonical form. Returned with
    the normalizations that changed something."""
    fired: list[str] = []
    if "model" in body:
        fired.append("model (rewritten by design)")
    if "stream" in body:
        fired.append("stream (the subject call always streams)")
    return _request_spellings(dialect, _without(body, ("model", "stream")), fired), fired


def _flat_tools(tools: list[Any]) -> list[Any]:
    """Responses tool declarations in canonical form: a namespace entry replaced by its members,
    each tagged with the group's name and description, gathered where the group first appears
    (an encoder renders one entry per group there, however many entries declared members);
    custom tools given their default format."""
    out: list[Any] = []
    groups: dict[str, list[Any]] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            out.append(tool)
            continue
        entry = cast("dict[str, Any]", tool)
        if entry.get("type") == "namespace":
            name = entry.get("name")
            members = groups.get(name) if isinstance(name, str) else None
            if members is None:
                members = []
                if isinstance(name, str):
                    groups[name] = members
                out.append(members)
            for member in cast("list[Any]", entry.get("tools") or []):
                if isinstance(member, dict):
                    member = dict(cast("dict[str, Any]", member))
                    member["namespace"] = name
                    member["namespace_description"] = entry.get("description") or ""
                    _default_custom_format(member)
                members.append(member)
            continue
        entry = dict(entry)
        _default_custom_format(entry)
        out.append(entry)
    flat: list[Any] = []
    for item in out:
        if isinstance(item, list):
            flat.extend(cast("list[Any]", item))
        else:
            flat.append(item)
    return flat


def _default_custom_format(entry: dict[str, Any]) -> None:
    if entry.get("type") == "custom" and not entry.get("format"):
        entry["format"] = {"type": "text"}


def _multi(pairs: Headers | QueryParams, *, fold: bool = False) -> dict[str, JSONValue]:
    """Name/value pairs as a name → list-of-values object, for comparison."""
    out: dict[str, list[str]] = {}
    for name, value in pairs:
        out.setdefault(name.lower() if fold else name, []).append(value)
    return cast("dict[str, JSONValue]", out)


def compare_response(
    dialect: Dialect, raw: JSONObject, encoded: JSONObject
) -> tuple[tuple[Difference, ...], tuple[str, ...]]:
    """The reconstruction check on the response side: the message the provider's frames
    described against the re-encoding of its decoding, on content, stop reason and usage.

    Ignored, and named in the returned list: generated identifiers (the message's own id, the
    ids of message and function-call items; reasoning-item and tool-call ids are compared), the
    echoed `model`, `type`/`role`/`object` envelope fields, `stop_sequence` and other
    null-valued fields (absent on the re-encoded side), empty citation, annotation and logprob
    lists, and JSON whitespace in re-encoded tool arguments.
    """
    ignored: list[str] = []
    left = _response_projection(dialect, raw, ignored)
    right = _response_projection(dialect, encoded, [])
    return tuple(_diff(left, right, "")), tuple(dict.fromkeys(ignored))


def _diff(raw: JSONValue, translated: JSONValue, path: str) -> list[Difference]:
    out: list[Difference] = []
    if isinstance(raw, dict) and isinstance(translated, dict):
        raw_dict = cast("dict[str, JSONValue]", raw)
        translated_dict = cast("dict[str, JSONValue]", translated)
        for key in sorted(raw_dict.keys() | translated_dict.keys()):
            here = f"{path}/{_escape(key)}"
            if key not in translated_dict:
                out.append(Difference(here, "missing_in_translated", raw_dict[key], None))
            elif key not in raw_dict:
                out.append(Difference(here, "extra_in_translated", None, translated_dict[key]))
            else:
                out.extend(_diff(raw_dict[key], translated_dict[key], here))
        return out
    if isinstance(raw, list) and isinstance(translated, list):
        raw_list = cast("list[JSONValue]", raw)
        translated_list = cast("list[JSONValue]", translated)
        # Element by element over the common length, then the tail by index: a dropped last
        # element reads as one missing entry rather than as two whole lists that differ.
        for index, (a, b) in enumerate(zip(raw_list, translated_list)):
            out.extend(_diff(a, b, f"{path}/{index}"))
        for index in range(len(translated_list), len(raw_list)):
            out.append(
                Difference(f"{path}/{index}", "missing_in_translated", raw_list[index], None)
            )
        for index in range(len(raw_list), len(translated_list)):
            out.append(
                Difference(f"{path}/{index}", "extra_in_translated", None, translated_list[index])
            )
        return out
    if not _same_scalar(raw, translated):
        out.append(Difference(path or "/", "value_differs", raw, translated))
    return out


def _same_scalar(raw: JSONValue, translated: JSONValue) -> bool:
    """JSON equality: a boolean is never a number, however Python compares them."""
    if isinstance(raw, bool) or isinstance(translated, bool):
        return type(raw) is type(translated) and raw == translated
    return raw == translated


def _escape(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _without(body: JSONObject, keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in body.items() if key not in keys}


def _has(node: Any, path: tuple[str, ...]) -> bool:
    for step in path:
        if not isinstance(node, dict) or step not in node:
            return False
        node = cast("dict[str, Any]", node)[step]
    return True


def _remove(node: dict[str, Any], path: tuple[str, ...]) -> None:
    key, rest = path[0], path[1:]
    if key not in node:
        return
    if not rest:
        del node[key]
        return
    child = node[key]
    if isinstance(child, dict):
        child = cast("dict[str, Any]", child)
        _remove(child, rest)
        if not child:
            del node[key]


def _text_block(text: str, kind: str = "text") -> dict[str, Any]:
    return {"type": kind, "text": text}


def _request_spellings(
    dialect: Dialect, body: dict[str, Any], ignored: list[str]
) -> dict[str, Any]:
    """The dialect's equivalent spellings of one request, in their canonical form. Each
    normalization that changed something is named in `ignored`."""
    if dialect == "anthropic_messages":
        # `disabled` and `between_tools` both mean no thinking before the response (the API
        # demands one or the other per model); the vocabulary holds the meaning, the codec
        # renders the canonical spelling, and the provider implementation resends with the
        # other when the model asks. Only this one leaf is normalized.
        thinking = body.get("thinking")
        if isinstance(thinking, dict):
            thinking_body = cast("dict[str, Any]", thinking)
            if thinking_body.get("type") == "between_tools":
                thinking_body["type"] = "disabled"
                ignored.append("thinking off spelling")
        # `eager_input_streaming` asks the API to stream a tool's input before the call is
        # complete; a hint about delivery, not about the tool. The vocabulary has no slot for
        # it (OpenCode sets it on every tool from 1.18.35), so the translated path does not
        # send it on, a known fidelity limit.
        for tool in _dicts(body.get("tools")):
            _pop(tool, "eager_input_streaming", ignored, "tool eager_input_streaming hint")
            # The API's defaults, spelled out or left out alike: a harness-run tool's `type`
            # (`custom`), who may call it (the model, `direct`), and loading it up front.
            _drop_nulls(tool, ignored, "null-valued tool fields")
            _pop_if(tool, "type", "custom", ignored, _TOOL_DEFAULTS)
            _pop_if(tool, "allowed_callers", ["direct"], ignored, _TOOL_DEFAULTS)
            _pop_if(tool, "defer_loading", False, ignored, _TOOL_DEFAULTS)
            _pop_if(tool, "strict", False, ignored, _TOOL_DEFAULTS)
        if isinstance(body.get("system"), str):
            body["system"] = [_text_block(cast("str", body["system"]))]
            ignored.append("string-vs-block content")
        # A turn-scoped system message (`clear_at: next_user_message`) renders nothing once a
        # later user message exists, and renders as any other system message until then.
        messages = body.get("messages")
        if isinstance(messages, list):
            kept = _without_cleared(cast("list[Any]", messages))
            if len(kept) < len(messages):
                body["messages"] = kept
                ignored.append("cleared turn-scoped system messages")
        for message in _dicts(body.get("messages")):
            if message.get("role") == "assistant":
                # The output configuration (effort) the API reported on its own message, echoed
                # by the harness on the assistant turn (Claude Code from 2.1.29x); the
                # vocabulary has no slot for it, so the translated path does not resend it (a
                # known fidelity limit).
                _pop(message, "output_config", ignored, "assistant message output_config")
            elif isinstance(message.get("output_config"), dict):
                output_config = cast("dict[str, Any]", message["output_config"])
                _drop_nulls(output_config, ignored, "null-valued message fields")
                _pop_if(message, "output_config", {}, ignored, "empty message output_config")
            _drop_nulls(message, ignored, "null-valued message fields")
            _pop_if(message, "clear_at", "never", ignored, "system message clear_at never")
            if message.get("role") == "system":
                _pop_if(
                    message,
                    "clear_at",
                    "next_user_message",
                    ignored,
                    "turn-scoped system message still shown",
                )
            if isinstance(message.get("content"), str):
                message["content"] = [_text_block(cast("str", message["content"]))]
                ignored.append("string-vs-block content")
            for block in _dicts(message.get("content")):
                # The Anthropic SDK serializes optional block fields as null (`citations: null`
                # on a text block echoed from a response); the API treats them as absent, and so
                # does the comparison.
                _drop_nulls(block, ignored, "null-valued block fields")
                if block.get("type") == "tool_result":
                    if isinstance(block.get("content"), str):
                        block["content"] = [_text_block(cast("str", block["content"]))]
                        ignored.append("string-vs-block content")
                    elif "content" not in block:
                        block["content"] = []  # no content and an empty list are one result
                        ignored.append("tool_result without content")
                    for part in _dicts(block.get("content")):
                        _drop_nulls(part, ignored, "null-valued block fields")
                if block.get("type") == "tool_use":
                    _pop(block, "caller", ignored, _CALLER)
        # A system-role message before any other is the system prompt: the vocabulary has one
        # system message either way, and the codec renders the leading ones as `system`. One
        # carrying more than its text (an effort change) is compared as itself.
        messages = body.get("messages")
        if isinstance(messages, list) and messages and _is_plain_system(messages[0]):
            system = cast("list[Any]", body.get("system") or [])
            while messages and _is_plain_system(messages[0]):
                leading = cast("dict[str, Any]", cast("list[Any]", messages).pop(0))
                system.extend(cast("list[Any]", leading.get("content") or []))
            body["system"] = system
            ignored.append("leading system messages as the system prompt")
        for block in _dicts(body.get("system")):
            _drop_nulls(block, ignored, "null-valued block fields")
    elif dialect == "openai_responses":
        # A null parameter is the API's default, as an absent one is; so is an empty
        # `reasoning` object (no effort, no summary).
        _drop_nulls(body, ignored, "null-valued top-level parameters")
        _pop_if(body, "reasoning", {}, ignored, "empty reasoning config")
        text = body.get("text")
        fmt = cast("dict[str, Any]", text).get("format") if isinstance(text, dict) else None
        if (
            isinstance(fmt, dict)
            and fmt.get("type") == "json_schema"
            and cast("dict[str, Any]", fmt).get("strict") is None
        ):
            # Absent or null is OpenAI's default; the codec says it outright.
            cast("dict[str, Any]", fmt)["strict"] = False
            ignored.append("json_schema format strict false by default")
        if body.get("service_tier") == "priority":
            body["service_tier"] = "fast"  # the older name of the same tier
            ignored.append("service_tier priority-vs-fast")
        if isinstance(body.get("input"), str):
            body["input"] = [
                {"role": "user", "content": [_text_block(cast("str", body["input"]), "input_text")]}
            ]
            ignored.append("string-vs-item input")
        # `instructions` is, by the API's definition, a developer message placed first in the
        # context, and a harness may spell its system prompt either way (Codex moved from the
        # field to a leading developer item at 0.160); the vocabulary has one system message
        # for both, so the comparison reads the field as that item.
        instructions = body.pop("instructions", None)
        if isinstance(instructions, str) and instructions:
            body["input"] = [
                {"role": "developer", "content": [_text_block(instructions, "input_text")]},
                *cast("list[Any]", body.get("input") or []),
            ]
        if instructions is not None:
            ignored.append("instructions as a leading developer item")
        # Tool declarations have one canonical place and shape: those an `additional_tools`
        # input item adds join the top-level list (the vocabulary keeps one tool list), a
        # `namespace` entry is its members each tagged with the group (the vocabulary keeps the
        # group on the tool), and a custom tool without a `format` means the text format.
        hoisted: list[Any] = []
        remaining: list[Any] = []
        additions = 0
        for raw_item in cast("list[Any]", body.get("input") or []):
            entry = cast("dict[str, Any]", raw_item) if isinstance(raw_item, dict) else None
            if entry is not None and entry.get("type") == "additional_tools":
                additions += 1
                hoisted.extend(cast("list[Any]", entry.get("tools") or []))
            else:
                remaining.append(raw_item)
        if additions:
            # Every addition item leaves the input, including one that added nothing.
            body["input"] = remaining
            ignored.append("additional_tools hoisted into tools")
        if hoisted:
            body["tools"] = [*cast("list[Any]", body.get("tools") or []), *hoisted]
        if isinstance(body.get("tools"), list):
            tools = cast("list[Any]", body["tools"])
            flat = _flat_tools(tools)
            if any(_dict_type(tool) == "namespace" for tool in tools):
                ignored.append("namespace entries flattened")
            if any(
                _dict_type(tool) == "custom" and not cast("dict[str, Any]", tool).get("format")
                for tool in [*tools, *_namespace_members(tools)]
            ):
                ignored.append("custom tool default format")
            body["tools"] = flat
            for tool in _dicts(flat):
                _drop_nulls(tool, ignored, "null-valued tool fields")
                _pop_if(tool, "allowed_callers", ["direct"], ignored, _TOOL_DEFAULTS)
                if tool.get("type") == "function" and "parameters" not in tool:
                    tool["parameters"] = {}  # a function without parameters takes no input
                    ignored.append("function tool without parameters")
        # An `allowed_tools` choice is its mode over the tools it allows: the API keeps the
        # others in the prompt (for its cache) but uncallable, the vocabulary's choices range
        # over all of the request's tools, and the codec leaves the others out (recording that
        # when there are any).
        choice = body.get("tool_choice")
        if isinstance(choice, dict) and _dict_type(choice) == "allowed_tools":
            choice_body = cast("dict[str, Any]", choice)
            body["tool_choice"] = "required" if choice_body.get("mode") == "required" else "auto"
            if isinstance(body.get("tools"), list):
                body["tools"] = [
                    tool
                    for tool in cast("list[Any]", body["tools"])
                    if _dict_type(tool) not in ("function", "custom")
                    or _is_allowed(choice_body, cast("dict[str, Any]", tool))
                ]
            ignored.append("allowed_tools as the tools it allows")
        leading = True  # every item so far is an instruction (system or developer) message
        for item in _dicts(body.get("input")):
            # Codex serializes optional item fields as null (`content: null` on a reasoning
            # item); the API treats them as absent, and so does the comparison.
            _drop_nulls(item, ignored, "null-valued item fields")
            kind = item.get("type", "message")
            leading = leading and kind == "message" and item.get("role") in ("system", "developer")
            if kind in ("function_call", "custom_tool_call", *_CALL_OUTPUTS):
                _pop_if(item, "caller", {"type": "direct"}, ignored, "direct tool caller")
            if kind == "message":
                _pop(item, "type", ignored, "message item type")
                # A message item's id is the provider's; an input message carries it only as
                # an echo. `call_id` is what pairs a call to its output, and is compared.
                _pop(item, "id", ignored, _INPUT_ITEM_IDS)
                _pop(item, "status", ignored, _ITEM_STATUS)
                # The phase the provider stamped on its own message (commentary or final
                # answer, gpt-6 era) is echoed by the harness; the vocabulary has no slot for
                # it, so the translated path does not resend it (a known fidelity limit).
                _pop(item, "phase", ignored, "assistant message phase")
                if isinstance(item.get("content"), str):
                    part = "output_text" if item.get("role") == "assistant" else "input_text"
                    item["content"] = [_text_block(cast("str", item["content"]), part)]
                    ignored.append("string-vs-item input")
                # The API's two instruction roles carry the same instructions before the
                # conversation (the vocabulary has one system message there); a later system
                # item is compared as itself, its role re-rendered as `developer` a recorded
                # loss.
                if item.get("role") == "system" and leading:
                    item["role"] = "developer"
                    ignored.append("system role as developer")
                # An assistant's text is text whichever part type spells it.
                if item.get("role") == "assistant":
                    for part in _dicts(item.get("content")):
                        if part.get("type") == "input_text":
                            part["type"] = "output_text"
                            ignored.append("assistant text part type")
            elif kind == "function_call":
                _pop(item, "id", ignored, _INPUT_ITEM_IDS)
                _pop(item, "status", ignored, _ITEM_STATUS)
                if _parse_arguments(item):
                    ignored.append("tool-argument whitespace")
            elif kind == "custom_tool_call":
                _pop(item, "id", ignored, _INPUT_ITEM_IDS)
                _pop(item, "status", ignored, _ITEM_STATUS)
            elif kind in _CALL_OUTPUTS:
                _pop(item, "id", ignored, _INPUT_ITEM_IDS)
                _pop(item, "status", ignored, _ITEM_STATUS)
                # The tool an output came from is the one its call names.
                if isinstance(item.get("call_id"), str):
                    _pop(item, "name", ignored, _OUTPUT_TOOL)
                    _pop(item, "namespace", ignored, _OUTPUT_TOOL)
                # A tool output of text parts only is the same as the joined string (the
                # vocabulary keeps the text), unless a part carries a cache breakpoint: then it
                # is the same list of `input_text` parts, the only type that carries one. A list
                # with any other part stays a list, so a dropped image or file reads as a
                # difference.
                output = item.get("output")
                if isinstance(output, list):
                    parts = _dicts(output)
                    if len(parts) == len(cast("list[Any]", output)) and all(
                        part.get("type") in ("input_text", "output_text", "text") for part in parts
                    ):
                        for part in parts:
                            _pop_if(part, "prompt_cache_breakpoint", None, ignored, _NULL_PARTS)
                        if not any("prompt_cache_breakpoint" in part for part in parts):
                            item["output"] = "".join(str(part.get("text", "")) for part in parts)
                            ignored.append("string-vs-part tool output")
                        else:
                            for part in parts:
                                if part.get("type") != "input_text":
                                    part["type"] = "input_text"
                                    ignored.append("cached tool output part type")
            elif kind == "reasoning":
                item_id = item.get("id")
                if isinstance(item_id, str) and item_id.startswith(SYNTHETIC_REASONING_ID_PREFIX):
                    del item["id"]
                    ignored.append("synthesized reasoning-item ids")
    elif dialect == "gemini_generate_content":
        _gemini_spellings(body, ignored)
    return body


def _gemini_spellings(body: dict[str, Any], ignored: list[str]) -> None:
    """The generateContent request's equivalent spellings (see `_request_spellings`)."""
    instruction = body.get("systemInstruction")
    if isinstance(instruction, str):
        body["systemInstruction"] = {"parts": [{"text": instruction}]} if instruction else {}
        ignored.append("string-vs-content system instruction")
    elif isinstance(instruction, dict):
        # The instruction is the system's whatever role it names; the API reads no role there.
        _pop(cast("dict[str, Any]", instruction), "role", ignored, "system instruction role")
    if body.get("systemInstruction") in ({}, {"parts": []}):
        del body["systemInstruction"]
        ignored.append("empty system instruction")
    # A content's role defaults to `user`, and consecutive contents of one role are one turn of
    # that side's (the vocabulary keeps the turns, not how a client split them); a content with
    # no parts says nothing.
    contents: list[dict[str, Any]] = []
    for content in _dicts(body.get("contents")):
        if not content.get("parts"):
            ignored.append("content without parts")
            continue
        if "role" not in content:
            content["role"] = "user"
            ignored.append("content role user by default")
        if contents and contents[-1].get("role") == content.get("role"):
            previous = cast("list[Any]", contents[-1].setdefault("parts", []))
            previous.extend(cast("list[Any]", content.get("parts") or []))
            ignored.append("consecutive contents of one role as one")
        else:
            contents.append(content)
    if isinstance(body.get("contents"), list):
        body["contents"] = contents
    for content in contents:
        for part in _dicts(content.get("parts")):
            call = part.get("functionCall")
            if isinstance(call, dict) and "args" not in call:
                cast("dict[str, Any]", call)["args"] = {}  # a call without arguments
                ignored.append("function call without args")
    _gemini_tool_spellings(body, ignored)
    for tool in _dicts(body.get("tools")):
        for declaration in _dicts(tool.get("functionDeclarations")):
            _pop_if(declaration, "behavior", "BLOCKING", ignored, "blocking function behavior")
            # The older `parameters` form (a Gemini Schema, an OpenAPI subset) is the same
            # declaration as JSON Schema, which is how the vocabulary keeps it.
            if "parameters" in declaration and "parametersJsonSchema" not in declaration:
                declaration["parametersJsonSchema"] = openapi_schema_to_json_schema(
                    declaration.pop("parameters")
                )
                ignored.append("legacy parameters schema as JSON Schema")
            if "response" in declaration and "responseJsonSchema" not in declaration:
                declaration["responseJsonSchema"] = openapi_schema_to_json_schema(
                    declaration.pop("response")
                )
                ignored.append("legacy response schema as JSON Schema")
    generation = body.get("generationConfig")
    if isinstance(generation, dict):
        generation = cast("dict[str, Any]", generation)
        _gemini_response_format_spellings(generation, ignored)
        _pop_if(generation, "stopSequences", [], ignored, "empty stop sequences")
        thinking = generation.get("thinkingConfig")
        if isinstance(thinking, dict):
            level = cast("dict[str, Any]", thinking).get("thinkingLevel")
            if isinstance(level, str) and level != level.upper():
                thinking["thinkingLevel"] = level.upper()  # the same level, in the enum's case
                ignored.append("thinking level case")
            # A budget of -1 is dynamic thinking, the API's default.
            _pop_if(
                cast("dict[str, Any]", thinking),
                "thinkingBudget",
                -1,
                ignored,
                "dynamic thinking budget",
            )
        if not generation:
            del body["generationConfig"]
            ignored.append("empty generation config")


def _gemini_response_format_spellings(generation: dict[str, Any], ignored: list[str]) -> None:
    """The response format's spellings, as `responseMimeType` with `responseJsonSchema`: the
    deprecated `_responseJsonSchema` and the OpenAPI `responseSchema` (as JSON Schema) are the
    same schema, the first one present in that order is the one the API
    applies, and `responseFormat.text` says the same as the two fields."""
    schema_keys = ("responseJsonSchema", "_responseJsonSchema", "responseSchema")
    present = (
        [key for key in schema_keys if key in generation]
        if generation.get("responseMimeType") == "application/json"
        else []
    )
    if present and present[0] != "responseJsonSchema":
        schema = generation[present[0]]
        generation["responseJsonSchema"] = (
            openapi_schema_to_json_schema(schema) if present[0] == "responseSchema" else schema
        )
        ignored.append(f"{present[0]} as responseJsonSchema")
    for key in present[1:]:
        generation.pop(key, None)
        ignored.append(f"{key} beside a schema that takes precedence")
    if present and present[0] != "responseJsonSchema":
        del generation[present[0]]
    response_format = generation.get("responseFormat")
    text = (
        cast("dict[str, Any]", response_format).get("text")
        if isinstance(response_format, dict)
        else None
    )
    if (
        isinstance(text, dict)
        and not any(key in generation for key in schema_keys)
        and "responseMimeType" not in generation
        and set(cast("dict[str, Any]", text)) <= {"mimeType", "schema"}
    ):
        text = cast("dict[str, Any]", text)
        mime = {"APPLICATION_JSON": "application/json", "TEXT_PLAIN": "text/plain"}.get(
            str(text.get("mimeType"))
        )
        if mime is not None:
            generation["responseMimeType"] = mime
            if text.get("schema") is not None:
                generation["responseJsonSchema"] = text["schema"]
            del cast("dict[str, Any]", response_format)["text"]
            if not response_format:
                del generation["responseFormat"]
            ignored.append("responseFormat.text as responseMimeType")


def _gemini_tool_spellings(body: dict[str, Any], ignored: list[str]) -> None:
    """Tool declarations and the tool choice: `allowedFunctionNames` is its mode over the
    functions it names (the vocabulary's choices range over all of the request's tools, and the
    codec leaves the others out, recording that when there are any; `ANY` over one function
    forces it, spelled the same), and an empty `tools[]` entry or tool config declares nothing."""
    config = body.get("toolConfig")
    if isinstance(config, dict):
        config = cast("dict[str, Any]", config)
        calling = config.get("functionCallingConfig")
        if isinstance(calling, dict):
            calling = cast("dict[str, Any]", calling)
            names = calling.get("allowedFunctionNames")
            mode = calling.get("mode")
            if isinstance(names, list) and not (
                mode == "ANY" and len(cast("list[Any]", names)) == 1
            ):
                allowed = set(cast("list[Any]", names))
                for tool in _dicts(body.get("tools")):
                    declarations = tool.get("functionDeclarations")
                    if isinstance(declarations, list):
                        tool["functionDeclarations"] = [
                            d for d in _dicts(declarations) if d.get("name") in allowed
                        ]
                del calling["allowedFunctionNames"]
                ignored.append("allowedFunctionNames as the functions it allows")
            has_functions = any(
                _dicts(tool.get("functionDeclarations")) for tool in _dicts(body.get("tools"))
            )
            if calling.get("mode") == "VALIDATED" and not has_functions:
                calling["mode"] = "AUTO"  # nothing to validate
                ignored.append("VALIDATED without functions as AUTO")
            if not calling:
                del config["functionCallingConfig"]
                ignored.append("empty function calling config")
        if not config:
            del body["toolConfig"]
            ignored.append("empty tool config")
    tools = body.get("tools")
    if isinstance(tools, list):
        kept = [
            tool
            for tool in cast("list[Any]", tools)
            if not (isinstance(tool, dict) and tool in ({}, {"functionDeclarations": []}))
        ]
        if len(kept) < len(cast("list[Any]", tools)):
            ignored.append("empty tool entries")
        if kept:
            body["tools"] = kept
        else:
            del body["tools"]


_CALLER = "tool_use caller attribution"
_INPUT_ITEM_IDS = "message, call and call-output input item ids"
_ITEM_STATUS = "echoed item status"
_TOOL_DEFAULTS = "tool fields at their defaults"
_OUTPUT_TOOL = "tool output's tool name (its call names it)"
_CALL_OUTPUTS = ("function_call_output", "custom_tool_call_output")
_NULL_PARTS = "null-valued part fields"


def _pop(node: dict[str, Any], key: str, ignored: list[str], label: str) -> None:
    if key in node:
        del node[key]
        ignored.append(label)


def _pop_if(node: dict[str, Any], key: str, value: Any, ignored: list[str], label: str) -> None:
    """Remove `key` when it holds `value` (the API's default, the same as leaving it out)."""
    if key in node and node[key] == value and type(node[key]) is type(value):
        del node[key]
        ignored.append(label)


def _is_allowed(choice: dict[str, Any], tool: dict[str, Any]) -> bool:
    """Whether an `allowed_tools` choice names a (flattened) tool: by name, in the namespace
    the entry gives or in any, or by a `namespace` entry naming its group."""
    for entry in _dicts(choice.get("tools")):
        name = entry.get("name")
        if entry.get("type") == "namespace":
            if isinstance(name, str) and tool.get("namespace") == name:
                return True
        elif name == tool.get("name") and entry.get("namespace") in (None, tool.get("namespace")):
            return True
    return False


def _is_plain_system(message: Any) -> bool:
    return _role(message) == "system" and set(cast("dict[str, Any]", message)) <= {
        "role",
        "content",
    }


def _without_cleared(messages: list[Any]) -> list[Any]:
    """The messages, less the turn-scoped system messages a later user message has cleared."""
    user_turns = [
        i
        for i, m in enumerate(messages)
        if isinstance(m, dict) and _role(m) not in ("assistant", "system")
    ]
    last_user = user_turns[-1] if user_turns else -1
    return [
        m
        for i, m in enumerate(messages)
        if not (
            isinstance(m, dict)
            and cast("dict[str, Any]", m).get("role") == "system"
            and cast("dict[str, Any]", m).get("clear_at") == "next_user_message"
            and i < last_user
        )
    ]


def _role(message: Any) -> Any:
    return cast("dict[str, Any]", message).get("role") if isinstance(message, dict) else None


def _drop_nulls(node: dict[str, Any], ignored: list[str], label: str) -> None:
    nulls = [key for key, value in node.items() if value is None]
    for key in nulls:
        del node[key]
    if nulls:
        ignored.append(label)


def _dict_type(value: Any) -> Any:
    return cast("dict[str, Any]", value).get("type") if isinstance(value, dict) else None


def _namespace_members(tools: list[Any]) -> list[Any]:
    return [
        member
        for tool in tools
        if _dict_type(tool) == "namespace"
        for member in cast("list[Any]", cast("dict[str, Any]", tool).get("tools") or [])
    ]


def _response_projection(
    dialect: Dialect, message: JSONObject, ignored: list[str]
) -> dict[str, Any]:
    """The part of a response the check compares, spelled canonically."""
    if dialect == "anthropic_messages":
        ignored.extend(
            [
                "message id and echoed model",
                "type/role envelope",
                "stop_sequence null-vs-absent",
                "empty citations",
                "tool_use caller attribution",
                "usage serving details (service_tier, inference_geo, cache_creation breakdown)",
                "stop_details null-vs-absent",
            ]
        )
        out: dict[str, Any] = {
            key: copy.deepcopy(message[key])
            for key in ("content", "stop_reason", "stop_sequence", "stop_details", "usage")
            if key in message
        }
        for key in ("stop_sequence", "stop_details"):
            if out.get(key) is None:
                out.pop(key, None)
        for block in _dicts(out.get("content")):
            if block.get("type") == "text" and not block.get("citations"):
                block.pop("citations", None)
            _drop_caller(block)
        usage = out.get("usage")
        if isinstance(usage, dict):
            for key in _ANTHROPIC_USAGE_SERVING_DETAILS:
                cast("dict[str, Any]", usage).pop(key, None)
        return out
    if dialect == "openai_responses":
        ignored.extend(
            [
                "response id and echoed model",
                "object/created_at envelope and request echoes",
                "message and function-call item ids",
                "null-valued item fields",
                "empty annotations and logprobs",
                "empty reasoning content",
                "item serving metadata",
                "message phase",
                "tool-argument whitespace",
            ]
        )
        out = {
            key: copy.deepcopy(message[key])
            for key in ("output", "status", "incomplete_details", "usage")
            if key in message
        }
        if out.get("incomplete_details") is None:
            out.pop("incomplete_details", None)
        for item in _dicts(out.get("output")):
            for key in [key for key, value in item.items() if value is None]:
                del item[key]
            # Serving metadata the API attaches to every item (turn ids, creation times): how
            # the call was served, not what it said.
            for key in _RESPONSES_ITEM_SERVING_METADATA:
                item.pop(key, None)
            if item.get("type") in ("message", "function_call", "custom_tool_call"):
                item.pop("id", None)
            if item.get("type") == "message":
                # The channel Codex shows the message on; the vocabulary has no slot for it, so
                # a re-encoded message never carries one (a documented loss on the translated
                # path, not a reconstruction defect).
                item.pop("phase", None)
            if item.get("type") == "reasoning" and not item.get("content"):
                # The reasoning-content slot next to `summary`, empty unless the request asked
                # for raw reasoning; an empty list and an absent field mean the same.
                item.pop("content", None)
            if item.get("type") == "function_call":
                _parse_arguments(item)
            for part in _dicts(item.get("content")):
                for key in ("annotations", "logprobs"):
                    if not part.get(key):
                        part.pop(key, None)
        return out
    if dialect == "gemini_generate_content":
        ignored.extend(
            [
                "response id and echoed model version",
                "candidate index, an unfiltered response's safety ratings, citation and logprob "
                "metadata",
                "usage breakdowns by modality and the tool-use prompt count",
            ]
        )
        candidates = _dicts(message.get("candidates"))
        candidate = candidates[0] if candidates else {}
        content = candidate.get("content")
        parts = cast("dict[str, Any]", content).get("parts") if isinstance(content, dict) else None
        out = {"parts": copy.deepcopy(parts or [])}
        if "finishReason" in candidate:
            out["finishReason"] = candidate["finishReason"]
        # What a filter said, for a filtered response (an ordinary one's safety ratings are not
        # carried); a blocked prompt is its feedback, with no candidate.
        feedback = message.get("promptFeedback")
        if isinstance(feedback, dict) and "blockReason" in feedback:
            out["promptFeedback"] = copy.deepcopy(feedback)
        if candidate.get("finishReason") in _GEMINI_FILTER_STOPS:
            for key in ("finishMessage", "safetyRatings"):
                if key in candidate:
                    out[key] = copy.deepcopy(candidate[key])
        usage = message.get("usageMetadata")
        if isinstance(usage, dict):
            usage = cast("dict[str, Any]", usage)
            # A count of zero and an absent one are the same (a blocked prompt has no candidate
            # tokens to count).
            out["usage"] = {
                key: usage[key] for key in _GEMINI_USAGE_COUNTS if usage.get(key) not in (None, 0)
            }
            if "toolUsePromptTokenCount" in usage:
                # The total counts the tool-use prompt, which the vocabulary's usage does not.
                out["usage"].pop("totalTokenCount", None)
        return out
    return {key: copy.deepcopy(value) for key, value in message.items()}


# Gemini finish reasons for a response a content filter stopped.
_GEMINI_FILTER_STOPS = (
    "SAFETY",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "SPII",
    "IMAGE_SAFETY",
    "RECITATION",
)

# The `usageMetadata` counts the vocabulary's usage carries.
_GEMINI_USAGE_COUNTS = (
    "promptTokenCount",
    "candidatesTokenCount",
    "thoughtsTokenCount",
    "cachedContentTokenCount",
    "totalTokenCount",
)


# Usage fields that describe how the provider served the call rather than what it cost: the
# service tier and inference region are operational, and `cache_creation` breaks the
# `cache_creation_input_tokens` total the vocabulary keeps down by TTL. A harness reads the
# totals.
_ANTHROPIC_USAGE_SERVING_DETAILS = ("service_tier", "inference_geo", "cache_creation")

# Fields the Responses API attaches to every output item to say how the call was served (the
# turn it belonged to, when the item was created), on reasoning, message and tool-call items
# alike. A harness echoes items back without them.
_RESPONSES_ITEM_SERVING_METADATA = ("metadata", "internal_chat_message_metadata_passthrough")


def _drop_caller(block: dict[str, Any]) -> None:
    """A tool_use block's `caller` says which agent issued the call (the model directly, or a
    server-side tool on its behalf); the vocabulary has one caller, the model, and the API
    accepts the block echoed back without it."""
    if block.get("type") == "tool_use":
        block.pop("caller", None)


def _parse_arguments(item: dict[str, Any]) -> bool:
    """Parse a call's JSON `arguments` string in place; whether it was one."""
    arguments = item.get("arguments")
    if isinstance(arguments, str):
        try:
            item["arguments"] = json.loads(arguments)
        except json.JSONDecodeError:
            return False
        return True
    return False


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        cast("dict[str, Any]", item) for item in cast("list[Any]", value) if isinstance(item, dict)
    ]
