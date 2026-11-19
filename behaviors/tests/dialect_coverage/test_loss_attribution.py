"""A loss the decoder records explains only the differences of the value it is about: a probe of
an `allowed_tools` choice (two declared tools, one allowed; and Gemini's `allowedFunctionNames`)
with a codec that also drops an unrelated field, or that does not apply the restriction, is
unhandled."""

import dataclasses
import json
from typing import Any

from behaviors.implementations.dialects.gemini_generate_content import GeminiGenerateContentCodec
from behaviors.implementations.dialects.openai_responses import OpenAIResponsesCodec
from behaviors.interfaces.dialect_codecs import DecodedRequest
from behaviors.types.dialect import Headers

from .audit import DialectAudit, Row, SchemaPath, probe, schema_paths
from .dialects import gemini_generate_content, openai_responses

_PATH = "tool_choice<type=allowed_tools>"


def _restricts(data: dict[str, Any]) -> bool:
    choice = data.get("tool_choice")
    return isinstance(choice, dict) and choice.get("type") == "allowed_tools"


class _DropsTemperature(OpenAIResponsesCodec):
    """Loses `temperature` whenever the choice restricts the tools (and so records a loss)."""

    def decode_request(self, target: str, headers: Headers, body: bytes) -> DecodedRequest:
        data = json.loads(body)
        if _restricts(data):
            data.pop("temperature", None)
        return super().decode_request(target, headers, json.dumps(data).encode())


class _IgnoresAllowedTools(OpenAIResponsesCodec):
    def decode_request(self, target: str, headers: Headers, body: bytes) -> DecodedRequest:
        data = json.loads(body)
        if _restricts(data):
            data["tool_choice"] = data["tool_choice"]["mode"]
        return super().decode_request(target, headers, json.dumps(data).encode())


def _probe(codec: OpenAIResponsesCodec | None = None) -> Row:
    spec = openai_responses()
    complete = spec.adjust
    assert complete is not None

    def adjust(body: dict[str, Any], pointer: str) -> tuple[Headers, str]:
        body["temperature"] = 0.5
        return complete(body, pointer)

    spec = dataclasses.replace(spec, codec=codec or spec.codec, adjust=adjust)
    return probe(spec, _path(spec))


def _path(spec: DialectAudit) -> SchemaPath:
    return next(path for path in schema_paths(spec.schema) if path.label == _PATH)


def test_the_restriction_is_a_known_loss() -> None:
    row = _probe()
    assert row.verdict == "known_loss"
    assert "allowed_tools: 1 declared tool(s) outside allowed_tools left out" in row.reasons


def test_an_unrelated_drop_is_unhandled_despite_the_recorded_loss() -> None:
    row = _probe(_DropsTemperature())
    assert row.verdict == "unhandled"
    assert "/temperature: missing from the re-encoding" in row.reasons


def test_a_restriction_not_applied_is_unhandled() -> None:
    row = _probe(_IgnoresAllowedTools())
    assert row.verdict == "unhandled"
    assert any(reason.startswith("/tools/1: added by the re-encoding") for reason in row.reasons)


class _GeminiIgnoresAllowedFunctions(GeminiGenerateContentCodec):
    def decode_request(self, target: str, headers: Headers, body: bytes) -> DecodedRequest:
        data = json.loads(body)
        calling = (data.get("toolConfig") or {}).get("functionCallingConfig") or {}
        calling.pop("allowedFunctionNames", None)
        return super().decode_request(target, headers, json.dumps(data).encode())


def _gemini_probe(codec: GeminiGenerateContentCodec | None = None) -> Row:
    spec = gemini_generate_content()
    spec = dataclasses.replace(spec, codec=codec or spec.codec)
    path = next(
        path
        for path in schema_paths(spec.schema)
        if path.label == "toolConfig.functionCallingConfig.allowedFunctionNames"
    )
    return probe(spec, path)


def test_a_gemini_function_restriction_is_a_known_loss() -> None:
    row = _gemini_probe()
    assert row.verdict == "known_loss"
    assert any(reason.startswith("allowed_tools:") for reason in row.reasons)


def test_a_gemini_function_restriction_not_applied_is_unhandled() -> None:
    row = _gemini_probe(_GeminiIgnoresAllowedFunctions())
    assert row.verdict == "unhandled"
