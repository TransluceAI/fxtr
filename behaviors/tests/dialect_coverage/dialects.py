"""The audited dialects: each codec with its pinned request schema and witness completions."""

import json
from collections.abc import Callable
from typing import Any, cast

import anthropic
import httpx2
import openai
from anthropic.types.beta.message_create_params import MessageCreateParamsBase
from openai.types.responses.response_create_params import ResponseCreateParamsBase

from behaviors.implementations.dialects.anthropic_messages import AnthropicMessagesCodec
from behaviors.implementations.dialects.gemini_generate_content import GeminiGenerateContentCodec
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesCodec
from behaviors.types.dialect import Dialect, Headers

from .audit import DialectAudit
from .schema import gemini_request_schema, gemini_schema_source, schema_source, typeddict_schema
from .wire import mark_header_parameters

# The SDK clients only ever talk to a recording transport.
_CLIENT_OPTIONS: dict[str, Any] = {
    "api_key": "probe",
    "base_url": "http://probe.invalid",
    "max_retries": 0,
}


def _complete_anthropic(body: dict[str, Any], pointer: str) -> tuple[Headers, str]:
    # The system-message TypedDict makes content optional, but the API requires either content
    # or output_config. A role-only witness needs text to be a valid message.
    for message in cast("list[dict[str, Any]]", body.get("messages") or []):
        if (
            message.get("role") == "system"
            and "content" not in message
            and "output_config" not in message
        ):
            message["content"] = "probe"
    # A tool choice is only rendered alongside the tools it chooses among.
    if "tool_choice" in body and not body.get("tools"):
        body["tools"] = [{"name": "probe", "input_schema": {"type": "object"}}]
    # Fast mode is a beta: the API takes `speed` only with its flag.
    if "speed" in body:
        return (("anthropic-beta", "fast-mode-2026-02-01"),), pointer
    return (), pointer


def _send_anthropic(http: httpx2.Client, body: dict[str, Any]) -> object:
    client = anthropic.Anthropic(**_CLIENT_OPTIONS, http_client=http)
    return cast("Any", client.beta.messages).create(**body)


def anthropic_messages() -> DialectAudit:
    # The beta request type is the superset: it adds the fields only a beta flag unlocks
    # (`context_management`, `mcp_servers`, ...), which harnesses send.
    root = MessageCreateParamsBase
    return DialectAudit(
        dialect="anthropic_messages",
        codec=AnthropicMessagesCodec(),
        target="/v1/messages",
        schema=mark_header_parameters(typeddict_schema(root), _send_anthropic),
        schema_source=f"{schema_source('anthropic')} {root.__module__}.{root.__qualname__}",
        sdk="anthropic",
        adjust=_complete_anthropic,
    )


def _complete_responses(body: dict[str, Any], pointer: str) -> tuple[Headers, str]:
    # A forced function or custom tool choice names a declared tool of its kind; an
    # `allowed_tools` choice allows one of two declared tools, so that it restricts something.
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and not body.get("tools"):
        choice = cast("dict[str, Any]", choice)
        kind = choice.get("type")
        if kind == "function":
            body["tools"] = [{"type": "function", "name": "probe", "parameters": {}}]
        elif kind == "custom":
            body["tools"] = [{"type": "custom", "name": "probe"}]
        elif kind == "allowed_tools":
            body["tools"] = [
                {"type": "function", "name": "probe", "parameters": {}},
                {"type": "function", "name": "other", "parameters": {}},
            ]
            allowed = choice.get("tools")
            if isinstance(allowed, list):
                cast("list[Any]", allowed).append({"type": "function", "name": "probe"})
    for item in cast("list[Any]", body.get("input") or []):
        if isinstance(item, dict):
            item = cast("dict[str, Any]", item)
            # Call arguments are a JSON document in a string.
            arguments = item.get("arguments")
            if isinstance(arguments, str) and not _is_json(arguments):
                item["arguments"] = json.dumps({"value": arguments})
            # The API's reasoning item ids start `rs_`, and the codec keeps only those.
            item_id = item.get("id")
            if item.get("type") == "reasoning" and isinstance(item_id, str):
                if not item_id.startswith("rs_"):
                    item["id"] = f"rs_{item_id}"
    # A freeform call's result is only told apart from a function call's by the call before it.
    items = cast("list[Any]", body.get("input") or [])
    if items and _item_type(items[0]) == "custom_tool_call_output":
        call_id = cast("dict[str, Any]", items[0]).get("call_id")
        call = {"type": "custom_tool_call", "call_id": call_id, "name": "probe", "input": "probe"}
        items.insert(0, call)
        if pointer.startswith("/input/0"):
            pointer = "/input/1" + pointer[len("/input/0") :]
    # And a freeform call is re-encoded as one only for a tool the request declares freeform, in
    # the namespace the call names.
    if not body.get("tools"):
        for item in items:
            if _item_type(item) == "custom_tool_call":
                call = cast("dict[str, Any]", item)
                tool = {"type": "custom", "name": call.get("name")}
                namespace = call.get("namespace")
                if isinstance(namespace, str):
                    tool = {
                        "type": "namespace",
                        "name": namespace,
                        "description": "probe",
                        "tools": [tool],
                    }
                body["tools"] = [tool]
                break
    return (), pointer


def _item_type(item: Any) -> Any:
    return cast("dict[str, Any]", item).get("type") if isinstance(item, dict) else None


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def _send_responses(http: httpx2.Client, body: dict[str, Any]) -> object:
    client = openai.OpenAI(**_CLIENT_OPTIONS, http_client=http)
    return cast("Any", client.responses).create(**body)


def openai_responses() -> DialectAudit:
    root = ResponseCreateParamsBase
    return DialectAudit(
        dialect="openai_responses",
        codec=OpenAIResponsesCodec(),
        target="/v1/responses",
        schema=mark_header_parameters(typeddict_schema(root), _send_responses),
        schema_source=f"{schema_source('openai')} {root.__module__}.{root.__qualname__}",
        sdk="openai",
        adjust=_complete_responses,
    )


def _complete_gemini(body: dict[str, Any], pointer: str) -> tuple[Headers, str]:
    # One candidate is what every response has; the setting means something from two on (one
    # more than the sampled value, which keeps the alternate value distinct too).
    if pointer == "/generationConfig/candidateCount":
        config = cast("dict[str, Any]", body["generationConfig"])
        config["candidateCount"] = cast("int", config["candidateCount"]) + 1
    # A response schema applies only under the JSON MIME type, which the API requires with it.
    config = body.get("generationConfig")
    if isinstance(config, dict):
        config = cast("dict[str, Any]", config)
        schema_keys = ("responseJsonSchema", "_responseJsonSchema", "responseSchema")
        if any(key in config for key in schema_keys):
            config.setdefault("responseMimeType", "application/json")
        text = cast("dict[str, Any]", config.get("responseFormat") or {}).get("text")
        if isinstance(text, dict) and "schema" in text:
            cast("dict[str, Any]", text)["mimeType"] = "APPLICATION_JSON"
    # A restriction to some functions restricts something: two are declared, one allowed.
    calling = cast("dict[str, Any]", (body.get("toolConfig") or {})).get("functionCallingConfig")
    if isinstance(calling, dict) and not body.get("tools"):
        names = cast("dict[str, Any]", calling).get("allowedFunctionNames")
        if isinstance(names, list):
            body["tools"] = [{"functionDeclarations": [{"name": "probe"}, {"name": "other"}]}]
            if "probe" not in names:
                cast("list[Any]", names).append("probe")
    # A function's result answers the model's call before it, and carries an output.
    contents = cast("list[Any]", body.get("contents") or [])
    for index, content in enumerate(contents):
        if not isinstance(content, dict) or cast("dict[str, Any]", content).get("role") == "model":
            continue
        for part_index, part in enumerate(
            cast("list[Any]", cast("dict[str, Any]", content).get("parts") or [])
        ):
            response = (
                cast("dict[str, Any]", part).get("functionResponse")
                if isinstance(part, dict)
                else None
            )
            if not isinstance(response, dict):
                continue
            response = cast("dict[str, Any]", response)
            here = f"/contents/{index}/parts/{part_index}/functionResponse/response"
            if response.get("response") == {} and not pointer.startswith(here):
                response["response"] = {"output": "probe"}
            function_call: dict[str, Any] = {"name": response.get("name", "probe"), "args": {}}
            if "id" in response:
                function_call["id"] = response["id"]  # a call and its result share an id
            call = {"role": "model", "parts": [{"functionCall": function_call}]}
            contents.insert(index, call)
            prefix = f"/contents/{index}"
            if pointer == prefix or pointer.startswith(prefix + "/"):
                pointer = f"/contents/{index + 1}" + pointer[len(prefix) :]
            return (), pointer
    return (), pointer


def gemini_generate_content() -> DialectAudit:
    return DialectAudit(
        dialect="gemini_generate_content",
        codec=GeminiGenerateContentCodec(),
        target="/v1beta/models/gemini-probe:streamGenerateContent?alt=sse",
        schema=gemini_request_schema(),
        schema_source=gemini_schema_source(),
        sdk=None,
        adjust=_complete_gemini,
    )


AUDITS: dict[Dialect, Callable[[], DialectAudit]] = {
    "anthropic_messages": anthropic_messages,
    "openai_responses": openai_responses,
    "gemini_generate_content": gemini_generate_content,
}
