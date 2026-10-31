"""Docent prompt bindings, schema guidance, and citation processing.

These helpers operate on the public ``docent-python`` SDK's context models.
Prompt bindings register inputs in an ``LLMContextSpec`` and replace template
parameters with aliases or literal text. Schema helpers control JSON output
instructions and resolve citation-enabled strings against the loaded context.

The corresponding contracts also exist in the Docent server's reading prompt
and reading worker; this module keeps local reading execution independent of
the server package.
"""

import json
import re
from typing import Any, cast

from docent.data_models.citation import parse_single_citation, scan_brackets
from docent.data_models.context_config import (
    AgentRunContextConfig,
    ParameterContextConfig,
    TranscriptContextConfig,
    TranscriptSliceContextConfig,
)
from docent.data_models.reading import ReadingParamType
from docent.sdk.llm_context import (
    AgentRunRef,
    LLMContext,
    LLMContextSpec,
    ReadingResultRef,
    TranscriptRef,
    TranscriptSliceRef,
    resolve_citations_with_context,
)


def _build_context_item_ref(
    param_type: ReadingParamType, value: dict[str, Any]
) -> AgentRunRef | TranscriptRef | TranscriptSliceRef | ReadingResultRef:
    if param_type == "agent_run":
        return AgentRunRef(
            id=value["id"],
            collection_id=value.get("collection_id", ""),
        )
    elif param_type == "transcript":
        return TranscriptRef(
            id=value["id"],
            agent_run_id=value.get("agent_run_id", ""),
            collection_id=value.get("collection_id", ""),
        )
    elif param_type == "transcript_slice":
        return TranscriptSliceRef(
            transcript_id=value["transcript_id"],
            start_idx=int(value["start_idx"]),
            end_idx=int(value["end_idx"]),
            agent_run_id=value.get("agent_run_id", ""),
            collection_id=value.get("collection_id", ""),
        )
    elif param_type == "reading_result":
        return ReadingResultRef(
            id=value["id"],
            collection_id=value.get("collection_id", ""),
        )
    raise ValueError(f"Unknown param_type: {param_type}")


_MAX_LIST_ITEMS = 1000


def _infer_param_type(param_name: str, value: Any) -> ReadingParamType:
    sample: Any
    if isinstance(value, list):
        list_value = cast(list[Any], value)
        sample = list_value[0] if list_value else None
    else:
        sample = value

    if isinstance(sample, dict):
        sample_dict = cast(dict[str, Any], sample)
        inferred: Any = sample_dict.get("type")
        if isinstance(inferred, str) and inferred in {
            "agent_run",
            "transcript",
            "transcript_slice",
            "reading_result",
        }:
            return cast(ReadingParamType, inferred)

    if isinstance(sample, str):
        return "text"

    raise ValueError(f"Unable to infer prompt parameter type for {param_name!r}")


def _register_ref_in_spec(
    spec: LLMContextSpec,
    ref: AgentRunRef | TranscriptRef | TranscriptSliceRef | ReadingResultRef,
    context_config: ParameterContextConfig | None = None,
) -> str:
    if isinstance(ref, AgentRunRef):
        if context_config is not None and not isinstance(context_config, AgentRunContextConfig):
            raise ValueError("agent_run parameters require AgentRunContextConfig")
        return spec.add_agent_run(
            id=ref.id,
            collection_id=ref.collection_id,
            context_config=context_config,
        )
    elif isinstance(ref, TranscriptRef):
        if context_config is not None and type(context_config) is not TranscriptContextConfig:
            raise ValueError("transcript parameters require TranscriptContextConfig")
        return spec.add_transcript(
            id=ref.id,
            agent_run_id=ref.agent_run_id,
            collection_id=ref.collection_id,
            is_root=True,
            context_config=context_config,
        )
    elif isinstance(ref, TranscriptSliceRef):
        if context_config is not None and not isinstance(
            context_config, TranscriptSliceContextConfig
        ):
            raise ValueError("transcript_slice parameters require TranscriptSliceContextConfig")
        return spec.add_transcript_slice(
            transcript_id=ref.transcript_id,
            start_idx=ref.start_idx,
            end_idx=ref.end_idx,
            agent_run_id=ref.agent_run_id,
            collection_id=ref.collection_id,
            context_config=context_config,
        )
    return spec.add_reading_result(
        id=ref.id,
        collection_id=ref.collection_id,
    )


def build_spec_for_row(
    prompt_template_segments: list[Any],
    arguments: dict[str, Any],
    context_configs: dict[str, ParameterContextConfig] | None = None,
) -> tuple[LLMContextSpec, list[str | dict[str, str]]]:
    """Build an LLMContextSpec and prompt segments for a single reading execution row.

    Args:
        prompt_template_segments: The raw template segments list (strings and
            parameter placeholder dicts).
        arguments: The resolved arguments dict mapping param names to context item values.
    """
    spec = LLMContextSpec()
    segments: list[str | dict[str, str]] = []
    rendered_param_map: dict[str, list[str | dict[str, str]]] = {}

    raw_segments: list[str | dict[str, str]] = prompt_template_segments  # type: ignore[assignment]
    for seg in raw_segments:
        if isinstance(seg, dict) and "param_name" in seg:
            param_name = seg["param_name"]
            param_type = cast(ReadingParamType, seg["param_type"])
            is_list = seg.get("is_list", False)
            if param_name in rendered_param_map:
                continue
            value: Any = arguments.get(param_name)
            if value is None:
                continue
            param_context_config = (context_configs or {}).get(param_name)

            if param_type == "unknown":
                param_type = _infer_param_type(param_name, value)
                is_list = isinstance(value, list)

            if param_type == "text":
                if is_list:
                    assert isinstance(value, list), f"Expected list for list param {param_name}"
                    text_items = cast(list[Any], value)
                    rendered_param_map[param_name] = [str(item) for item in text_items]
                else:
                    rendered_param_map[param_name] = [str(value)]
                continue

            if is_list:
                assert isinstance(value, list), f"Expected list for list param {param_name}"
                items: list[dict[str, Any] | str] = value
                assert len(items) <= _MAX_LIST_ITEMS, (
                    f"List param {param_name} exceeds {_MAX_LIST_ITEMS} items"
                )
                aliases: list[str] = []
                for item in items:
                    if isinstance(item, list):
                        raise ValueError(
                            f"Nested list values are not supported for parameter {param_name}"
                        )
                    item_dict: dict[str, Any] = (
                        item if isinstance(item, dict) else {"id": str(item)}
                    )
                    ref = _build_context_item_ref(param_type, item_dict)
                    aliases.append(_register_ref_in_spec(spec, ref, param_context_config))
                rendered_param_map[param_name] = [{"alias": alias} for alias in aliases]
            else:
                assert not isinstance(value, list), f"Expected scalar for param {param_name}"
                value_dict: dict[str, Any] = (
                    cast(dict[str, Any], value) if isinstance(value, dict) else {"id": str(value)}
                )
                ref = _build_context_item_ref(param_type, value_dict)
                rendered_param_map[param_name] = [
                    {"alias": _register_ref_in_spec(spec, ref, param_context_config)}
                ]

    for seg in raw_segments:
        if isinstance(seg, str):
            segments.append(seg)
        elif "param_name" in seg:
            rendered_param = rendered_param_map.get(seg["param_name"])
            if rendered_param is not None:
                segments.extend(rendered_param)

    return spec, segments


def is_default_output_schema(output_schema: dict[str, Any]) -> bool:
    """True when the schema is the built-in single-string wrapper: `{output: string}`.

    Default-schema readings skip JSON-structured output parsing — the raw LLM
    text is wrapped in `{"output": raw_text}` directly.
    """
    if output_schema.get("type") != "object":
        return False
    properties = output_schema.get("properties", {})
    if set(properties.keys()) != {"output"}:
        return False
    output_prop = properties.get("output", {})
    if output_prop.get("type") != "string":
        return False
    allowed_keys = {"type", "citations"}
    return set(output_prop.keys()).issubset(allowed_keys)


def schema_wants_citations(output_schema: dict[str, Any]) -> bool:
    """Recursively check whether any string field in the schema has `"citations": true`."""
    if output_schema.get("type") == "string" and output_schema.get("citations") is True:
        return True
    for prop in output_schema.get("properties", {}).values():
        if schema_wants_citations(prop):
            return True
    if "items" in output_schema:
        if schema_wants_citations(output_schema["items"]):
            return True
    return False


def make_schema_system_message(output_schema: dict[str, Any]) -> str:
    schema_str = json.dumps(output_schema, indent=2)
    return (
        "You must respond with valid JSON that conforms to the following JSON schema:"
        f"\n\n{schema_str}\n\nRespond with only the JSON object and nothing else."
    )


_CITATION_TRAILING_PUNCT_RE = re.compile(r"\s*([,.;:!?])")


def strip_citation_syntax(text: str) -> str:
    # Use scan_brackets for code-point offsets (parse_citations returns UTF-16 offsets).
    brackets = scan_brackets(text)
    stripped = text
    for start, end, content in reversed(brackets):
        if not parse_single_citation(content):
            continue
        # If the citation is followed by (optional whitespace +) sentence
        # punctuation, also drop any whitespace immediately before the bracket
        # so the surviving punctuation sits flush against the prior text.
        # Otherwise just remove the bracket and leave surrounding spacing alone.
        m = _CITATION_TRAILING_PUNCT_RE.match(stripped, end)
        if m is None:
            stripped = stripped[:start] + stripped[end:]
            continue
        before = start
        while before > 0 and stripped[before - 1].isspace():
            before -= 1
        stripped = stripped[:before] + stripped[m.start(1) :]
    return stripped


def process_citation_fields(output: Any, schema: dict[str, Any], context: LLMContext) -> Any:
    if schema.get("type") == "string":
        if schema.get("citations") is True:

            def _resolve_citation_field(text: str) -> dict[str, Any]:
                cleaned, cites = resolve_citations_with_context(text, context)
                return {
                    "text": cleaned,
                    "citations": [c.model_dump(mode="json") for c in cites],
                }

            return _resolve_citation_field(output)
        return strip_citation_syntax(output)

    if schema.get("type") == "object":
        properties: dict[str, Any] = schema.get("properties", {})
        result: dict[str, Any] = {}
        for key in properties:
            if key in output:
                result[key] = process_citation_fields(output[key], properties[key], context)
        return result

    if schema.get("type") == "array":
        item_schema: dict[str, Any] = schema.get("items", {})
        return [process_citation_fields(item, item_schema, context) for item in output]

    return output
