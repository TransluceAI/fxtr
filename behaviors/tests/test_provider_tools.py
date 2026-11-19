"""Each harness-side codec reports the provider-side tools a request declares (the policy check
reads these before anything is sent) and lists what its decoder left out of the vocabulary."""

import json
from typing import Any, cast

from behaviors.implementations.dialects.anthropic_messages import AnthropicMessagesCodec
from behaviors.implementations.dialects.gemini_generate_content import GeminiGenerateContentCodec
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesCodec
from behaviors.types.json import JSONObject
from behaviors.types.provider_tool import ProviderTool


def _pairs(tools: tuple[ProviderTool, ...]) -> list[tuple[str, str]]:
    return [(tool.family, tool.name) for tool in tools]


def _tools_body(*tools: dict[str, Any]) -> JSONObject:
    return cast("JSONObject", {"tools": list(tools)})


def _capabilities(dropped: tuple[object, ...]) -> dict[str, str]:
    return {getattr(d, "code"): getattr(d, "detail") for d in dropped}


# --- Anthropic ---------------------------------------------------------------------------------


def test_anthropic_provider_tools_by_type_prefix_and_mcp_servers() -> None:
    body: JSONObject = {
        "tools": [
            {"name": "Bash", "input_schema": {}},
            {"type": "custom", "name": "Edit", "input_schema": {}},
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
            {"type": "web_fetch_20250910", "name": "web_fetch"},
            {"type": "code_execution_20250825", "name": "code_execution"},
            {"type": "mcp_toolset", "mcp_server_name": "gh"},
            {"type": "bash_20250124", "name": "bash"},  # the harness runs it
            {"type": "tool_search_tool_regex_20251119", "name": "tool_search"},  # no reach
            {"type": "frobnicator_20260101", "name": "frob"},  # unrecognized: fails closed
        ],
        "mcp_servers": [{"type": "url", "name": "github", "url": "https://mcp.example"}],
    }
    assert _pairs(AnthropicMessagesCodec().provider_tools(body)) == [
        ("web_search", "web_search_20250305"),
        ("web_fetch", "web_fetch_20250910"),
        ("code_execution", "code_execution_20250825"),
        ("mcp", "mcp_toolset"),
        ("unknown", "frobnicator_20260101"),
        ("mcp", "mcp_servers:github"),
    ]
    assert AnthropicMessagesCodec().provider_tools({"model": "m", "messages": []}) == ()


def test_anthropic_decoder_lists_what_it_left_out() -> None:
    body: dict[str, Any] = {
        "model": "claude-harness",
        "max_tokens": 10,
        "tools": [
            {"name": "Bash", "input_schema": {}},
            {"type": "web_search_20250305", "name": "web_search"},
            {"type": "text_editor_20250124", "name": "str_replace_editor"},
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "srvtoolu_1",
                        "name": "web_search",
                        "input": {"query": "x"},
                    },
                    {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []},
                    {"type": "text", "text": "found"},
                ],
            },
        ],
    }
    decoded = AnthropicMessagesCodec().decode_request("/v1/messages", (), json.dumps(body).encode())
    dropped = _capabilities(decoded.decode_losses)
    assert set(dropped) == {
        "provider_tool:web_search",
        "builtin_tool",
        "image",
        "provider_tool_exchange",
    }
    assert (
        dropped["builtin_tool"]
        == "provider-defined tool declared without a schema: text_editor_20250124"
    )
    assert dropped["image"] == "1 image block(s)"
    assert (
        dropped["provider_tool_exchange"]
        == "2 provider-side tool call/result block(s) in the history"
    )
    assert [tool.name for tool in decoded.request.tools] == ["Bash"]
    plain = {"model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}
    assert (
        AnthropicMessagesCodec()
        .decode_request("/v1/messages", (), json.dumps(plain).encode())
        .decode_losses
        == ()
    )


# --- Responses ---------------------------------------------------------------------------------


def test_responses_provider_tools_by_type() -> None:
    body: JSONObject = {
        "tools": [
            {"type": "function", "name": "shell", "parameters": {}},
            {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
            {"type": "namespace", "name": "multi_agent", "tools": []},
            {"type": "web_search", "filters": {"allowed_domains": ["example.com"]}},
            {"type": "web_search_preview"},
            {"type": "code_interpreter", "container": {"type": "auto"}},
            {"type": "file_search", "vector_store_ids": ["vs_1"]},
            {"type": "image_generation"},
            {"type": "mcp", "server_label": "deepwiki", "server_url": "https://mcp.example"},
            {"type": "local_shell"},  # the harness runs it
            {"type": "computer_use_preview", "display_width": 1, "display_height": 1},
            {"type": "tool_search"},  # provider-run, but over the request's own tools only
            {"type": "teleporter"},  # unrecognized: fails closed
        ]
    }
    assert _pairs(OpenAIResponsesCodec().provider_tools(body)) == [
        ("web_search", "web_search"),
        ("web_search", "web_search_preview"),
        ("code_execution", "code_interpreter"),
        ("file_search", "file_search"),
        ("image_generation", "image_generation"),
        ("mcp", "mcp:deepwiki"),
        ("unknown", "teleporter"),
    ]


def test_responses_decoder_lists_what_it_left_out() -> None:
    body: dict[str, Any] = {
        "model": "gpt-harness",
        "tools": [
            {"type": "function", "name": "shell", "parameters": {}},
            {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
            {"type": "namespace", "name": "multi_agent", "tools": []},
            {"type": "web_search"},
            {"type": "local_shell"},
            {"type": "tool_search"},
            {"type": "function", "name": "rare", "parameters": {}, "defer_loading": True},
        ],
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "look"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
                ],
            },
            {
                "type": "custom_tool_call",
                "call_id": "c1",
                "name": "apply_patch",
                "input": "*** Begin Patch",
            },
            {"type": "custom_tool_call_output", "call_id": "c1", "output": "ok"},
            {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "action": {"type": "search"},
            },
            {
                "type": "local_shell_call",
                "call_id": "ls_1",
                "action": {"type": "exec", "command": ["ls"]},
            },
            {"type": "tool_search_call", "id": "ts_1", "status": "completed"},
            {"type": "additional_tools", "tools": []},
        ],
    }
    decoded = OpenAIResponsesCodec().decode_request("/v1/responses", (), json.dumps(body).encode())
    dropped = _capabilities(decoded.decode_losses)
    # Custom tools, namespaces and `additional_tools` are carried by the vocabulary now, so they
    # are not losses; what remains is what still has no slot.
    assert set(dropped) == {
        "provider_tool:web_search",
        "builtin_tool",
        "provider_tool_exchange",
        "tool_search",
        "image",
    }
    tools = {tool.name: tool for tool in decoded.request.tools}
    assert tools["apply_patch"].freeform_format == {"type": "text"}
    assert set(tools) == {"shell", "apply_patch", "rare"}
    assert dropped["tool_search"] == (
        "the tool_search tool; 1 tool definition(s) marked defer_loading; "
        "1 tool_search call item(s) in the history"
    )
    assert (
        dropped["provider_tool_exchange"]
        == "1 provider-side tool call/result item(s) in the history"
    )
    assert dropped["image"] == "1 image part(s)"


# --- Gemini ------------------------------------------------------------------------------------


def test_gemini_provider_tools_by_key() -> None:
    body: JSONObject = {
        "tools": [
            {"functionDeclarations": [{"name": "read_file", "parameters": {}}]},
            {"googleSearch": {}},
            {"googleSearchRetrieval": {"dynamicRetrievalConfig": {}}},
            {"urlContext": {}},
            {"codeExecution": {}},
            {"fileSearch": {"fileSearchStoreNames": ["x"]}},
            {"computerUse": {"environment": "ENVIRONMENT_BROWSER"}},  # the harness runs it
            {"mystery": {}},
        ]
    }
    assert _pairs(GeminiGenerateContentCodec().provider_tools(body)) == [
        ("web_search", "googleSearch"),
        ("web_search", "googleSearchRetrieval"),
        ("web_fetch", "urlContext"),
        ("code_execution", "codeExecution"),
        ("file_search", "fileSearch"),
        ("unknown", "mystery"),
    ]


def test_gemini_decoder_lists_what_it_left_out() -> None:
    body: dict[str, Any] = {
        "tools": [
            {"functionDeclarations": [{"name": "read_file"}]},
            {"googleSearch": {}},
            {"computerUse": {}},
        ],
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "look"},
                    {"inlineData": {"mimeType": "image/png", "data": "AA=="}},
                    {"fileData": {"mimeType": "application/pdf", "fileUri": "gs://x"}},
                ],
            },
            {
                "role": "model",
                "parts": [
                    {"executableCode": {"language": "PYTHON", "code": "1+1"}},
                    {"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "2"}},
                    {"text": "two"},
                ],
            },
        ],
    }
    decoded = GeminiGenerateContentCodec().decode_request(
        "/v1beta/models/gemini-2.5-pro:generateContent", (), json.dumps(body).encode()
    )
    dropped = _capabilities(decoded.decode_losses)
    assert set(dropped) == {
        "provider_tool:web_search",
        "builtin_tool",
        "image",
        "document",
        "provider_tool_exchange",
    }
    assert dropped["provider_tool_exchange"] == "2 code-execution part(s) in the history"
    assert dropped["document"] == "1 file data part(s)"


# --- the reviewer's three dimensions: tool location, execution location, media location --------


def test_responses_shell_is_classified_by_where_it_runs() -> None:
    codec = OpenAIResponsesCodec()
    local = {"type": "shell", "environment": {"type": "local"}}
    hosted = {"type": "shell", "environment": {"type": "container_auto"}}
    referenced = {
        "type": "shell",
        "environment": {"type": "container_reference", "container_id": "c"},
    }
    bare = {"type": "shell"}
    odd = {"type": "shell", "environment": {"type": "teleporter"}}
    assert codec.provider_tools(_tools_body(local)) == ()
    assert _pairs(codec.provider_tools(_tools_body(hosted, referenced))) == [
        ("code_execution", "shell:container_auto"),
        ("code_execution", "shell:container_reference"),
    ]
    # No environment, or one the codec does not know, fails closed.
    assert _pairs(codec.provider_tools(_tools_body(bare, odd))) == [
        ("unknown", "shell:None"),
        ("unknown", "shell:teleporter"),
    ]
    body: dict[str, Any] = {"model": "m", "input": "hi", "tools": [local]}
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    assert _capabilities(decoded.decode_losses) == {
        "builtin_tool": "API-defined tool the harness runs: shell:local"
    }


def test_responses_scan_covers_every_declaration_location() -> None:
    codec = OpenAIResponsesCodec()
    body: dict[str, Any] = {
        "model": "m",
        "tools": [
            {"type": "function", "name": "f", "parameters": {}},
            {"type": "namespace", "name": "ns", "tools": [{"type": "web_search_preview"}]},
        ],
        "input": [
            {"type": "message", "role": "user", "content": "hi"},
            {
                "type": "additional_tools",
                "tools": [
                    {"type": "web_search"},
                    {"type": "function", "name": "late", "parameters": {}},
                ],
            },
        ],
    }
    assert _pairs(codec.provider_tools(body)) == [
        ("web_search", "web_search_preview"),
        ("web_search", "web_search"),
    ]
    decoded = codec.decode_request("/v1/responses", (), json.dumps(body).encode())
    dropped = _capabilities(decoded.decode_losses)
    # The top-level list (with the namespace's member) is converted before the addition's.
    assert dropped["provider_tool:web_search"] == (
        "provider-side tool web_search_preview; provider-side tool web_search"
    )
    assert "additional_tools" not in dropped
    assert [tool.name for tool in decoded.request.tools] == ["f", "late"]


def test_responses_media_in_tool_results_is_recorded_like_media_in_messages() -> None:
    codec = OpenAIResponsesCodec()
    body: dict[str, Any] = {
        "model": "m",
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "screenshot", "arguments": "{}"},
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [
                    {"type": "input_text", "text": "here"},
                    {"type": "input_image", "image_url": "data:image/png;base64,AA=="},
                    {"type": "input_file", "filename": "a.pdf", "file_data": "AA=="},
                ],
            },
        ],
        "tools": [{"type": "function", "name": "screenshot", "parameters": {}}],
    }
    dropped = _capabilities(
        codec.decode_request("/v1/responses", (), json.dumps(body).encode()).decode_losses
    )
    assert dropped == {"image": "1 image part(s)", "document": "1 file part(s)"}


def test_anthropic_media_in_messages_and_tool_results_is_recorded() -> None:
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
    }
    body: dict[str, Any] = {
        "model": "m",
        "max_tokens": 1,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "look"}, image]},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "shot", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            image,
                            {
                                "type": "document",
                                "source": {"type": "text", "media_type": "text/plain", "data": "x"},
                            },
                        ],
                    }
                ],
            },
        ],
    }
    decoded = AnthropicMessagesCodec().decode_request("/v1/messages", (), json.dumps(body).encode())
    assert _capabilities(decoded.decode_losses) == {
        "image": "2 image block(s)",
        "document": "1 document block(s)",
    }


# --- the invariant: a child of a recognized container is converted or reported ----------------
#
# The same image / audio / file payloads, in every location each dialect accepts content: a
# user message, an assistant message, a tool result and the system prompt. Reading one field of
# a container must not count as handling the container.

_PNG = "AA=="


def test_gemini_media_is_recorded_in_every_part_location() -> None:
    image = {"inlineData": {"mimeType": "image/png", "data": _PNG}}
    audio = {"inlineData": {"mimeType": "audio/wav", "data": _PNG}}
    pdf = {"fileData": {"mimeType": "application/pdf", "fileUri": "gs://b/a.pdf"}}
    body: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": "sys"}, image]},
        "contents": [
            {"role": "user", "parts": [{"text": "look"}, image, audio, pdf]},
            {
                "role": "model",
                "parts": [{"functionCall": {"name": "shot", "args": {}}}, image],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "shot",
                            "response": {"output": "done"},
                            "parts": [image, audio, {"text": " (see attached)"}],
                        }
                    }
                ],
            },
        ],
    }
    decoded = GeminiGenerateContentCodec().decode_request(
        "/v1beta/models/gemini-2.5-pro:generateContent", (), json.dumps(body).encode()
    )
    assert _capabilities(decoded.decode_losses) == {
        "image": "4 inline image part(s)",
        "audio": "2 inline audio part(s)",
        "document": "1 file data part(s)",
    }
    # The function response's text child is kept, alongside its result data.
    tool_messages = [m for m in decoded.request.context if m.role == "tool"]
    assert tool_messages[0].content[0].text == "done (see attached)"


def test_anthropic_media_is_recorded_in_every_block_location() -> None:
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG}}
    pdf = {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": _PNG},
    }
    cited = {
        "type": "text",
        "text": "quoted",
        "citations": [{"type": "char_location", "cited_text": "q", "document_index": 0}],
    }
    body: dict[str, Any] = {
        "model": "m",
        "max_tokens": 1,
        "system": [{"type": "text", "text": "sys"}, image, {"type": "mystery"}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "look"}, image, pdf]},
            {
                "role": "assistant",
                "content": [cited, {"type": "tool_use", "id": "t1", "name": "shot", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [image, pdf]}],
            },
        ],
    }
    decoded = AnthropicMessagesCodec().decode_request("/v1/messages", (), json.dumps(body).encode())
    assert _capabilities(decoded.decode_losses) == {
        "image": "3 image block(s)",
        "block:mystery": "1 'mystery' block(s)",
        "document": "2 document block(s)",
        "citations": "1 text block citation list(s)",
    }


def test_responses_media_is_recorded_in_every_part_location() -> None:
    image = {"type": "input_image", "image_url": f"data:image/png;base64,{_PNG}"}
    audio = {"type": "input_audio", "input_audio": {"data": _PNG, "format": "wav"}}
    pdf = {"type": "input_file", "filename": "a.pdf", "file_data": _PNG}
    body: dict[str, Any] = {
        "model": "m",
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "sys"}, pdf],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "look"}, image, audio],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "see [1]",
                        "annotations": [{"type": "url_citation", "url": "https://x"}],
                    },
                    {"type": "refusal", "refusal": "no"},
                ],
            },
            {"type": "function_call", "call_id": "c1", "name": "shot", "arguments": "{}"},
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [{"type": "input_text", "text": "ok"}, image, pdf],
            },
        ],
        "tools": [{"type": "function", "name": "shot", "parameters": {}}],
    }
    decoded = OpenAIResponsesCodec().decode_request("/v1/responses", (), json.dumps(body).encode())
    assert _capabilities(decoded.decode_losses) == {
        "image": "2 image part(s)",
        "audio": "1 audio part(s)",
        "document": "2 file part(s)",
        "citations": "1 text part annotation list(s)",
        "refusal": "1 refusal part(s) in the history, sent as text",
    }
