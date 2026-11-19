"""Adaptation steps for structured-output contracts: response formats, strict tools and tool
output schemas, fitted to what a dialect can carry and enforce.

These are building blocks for `ModelSideCodec.adapt_request`, alongside the general ones in
`dialects.adaptation`: each codec calls the ones its dialect needs, and each records a
`RequestLoss` at the point it changes the request. Every contract loss is semantic: the model is
bound by something other than what the request asked for (a schema enforced in part, not at all,
or more strictly). Under `AdaptationPolicy(semantic_changes="refuse")` the call fails instead of
being sent with one. Which schemas each provider enforces is `schema_support`'s.

A schema the target cannot enforce as given is fitted so that the fewest of its constraints go
unenforced:

- A keyword that constrains only the value of its own node and holds no subschema (a string
  `format`, `uniqueItems`) moves into that node's description, where the model still reads it.
  Only that constraint goes unenforced, and the rest of the schema stays enforced.
- A problem that only an added constraint would fix (an optional property, an object that allows
  extra keys, a node without a type) cannot be fitted without rejecting answers the request
  allowed. The schema is sent unenforced where the target has an unenforced mode (OpenAI's
  `strict: false`). Anthropic has none, so its schema is fitted the way the Anthropic SDK fits
  one: open objects are closed, and a schema it would still reject is left out.

The codes:

- Response format: `response_format_unenforced` (a strict schema sent without strict mode),
  `response_format_dropped` (a schema the target would reject even unenforced, left out),
  `response_format_strengthened` (an advisory schema to a provider that always enforces),
  `response_schema_weakened` (naming what the target cannot enforce), `response_schema_strengthened`
  (objects that allowed extra keys made closed), `response_format_description` (a description
  the target has no slot for), `json_mode` (JSON without a schema, which the target cannot ask
  for).
- Tools: `tool_strict_unenforced`, `tool_strict_strengthened`, `tool_schema_weakened`,
  `tool_output_schema` (a declaration the target has no slot for), and `tool_result_shape` (one
  left out because the tool's results in the context would not match it).
"""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from behaviors.implementations.dialects.adaptation import AdaptedRequestBuilder
from behaviors.implementations.dialects.schema_support import (
    SchemaTarget,
    TransformedSchema,
    anthropic_rejections,
    anthropic_transform,
    gemini_unvalidated_keywords,
    is_plain_object_root,
    move_keywords,
    openai_strict_movable,
    openai_strict_violations,
    unsupported_keywords,
)
from behaviors.types.json import JSONObject
from behaviors.types.response import ResponseFormatJsonObject, ResponseFormatJsonSchema
from behaviors.types.tool import ToolChoiceAuto, ToolDefinition

ENFORCEMENT_CODES = frozenset(
    {
        "response_format_unenforced",
        "response_format_dropped",
        "response_schema_weakened",
        "response_schema_strengthened",
        "tool_strict_unenforced",
        "tool_strict_strengthened",
        "tool_schema_weakened",
    }
)
"""The codes for what a provider enforces of a schema, as opposed to what its dialect can say:
a request in the provider's own dialect meets these limits too (a schema the provider rejects
in strict mode, keywords it ignores), so they are recorded even within one dialect."""


def strict_problems(target: SchemaTarget | None) -> Callable[[JSONObject], tuple[str, ...]]:
    """Why a schema cannot be enforced strictly by `target` (empty when it can): OpenAI's strict
    rules for None, else the keywords outside the target's supported subset."""
    if target is None:
        return openai_strict_violations

    def unsupported(schema: JSONObject) -> tuple[str, ...]:
        keywords = unsupported_keywords(schema, target)
        found = (f"unsupported keywords: {', '.join(keywords)}",) if keywords else ()
        return found + (anthropic_rejections(schema) if target == "anthropic" else ())

    return unsupported


# --- response format ------------------------------------------------------------------------------


def drop_json_mode(adaptation: AdaptedRequestBuilder) -> None:
    """Leave out a schema-free JSON response format, for a dialect that can only ask for JSON
    with a schema. No schema is made up: `{"type": "object"}` would be refused by a provider
    that requires every object closed, and closing it would allow only `{}`."""
    if isinstance(adaptation.request.response_format, ResponseFormatJsonObject):
        adaptation.note("json_mode", "JSON without a schema: the format is left out")
        adaptation.request = replace(adaptation.request, response_format=None)


def enforce_response_format(adaptation: AdaptedRequestBuilder) -> None:
    """For a dialect that always enforces a response schema and has no slot for its description:
    an advisory schema becomes enforced, and the description is left out."""
    fmt = adaptation.request.response_format
    if not isinstance(fmt, ResponseFormatJsonSchema):
        return
    if not fmt.strict:
        adaptation.note(
            "response_format_strengthened", "an advisory schema is enforced by this provider"
        )
    if fmt.description is not None:
        adaptation.note("response_format_description", "the format's description has no slot")
    if not fmt.strict or fmt.description is not None:
        adaptation.request = replace(
            adaptation.request, response_format=replace(fmt, strict=True, description=None)
        )


def fit_response_schema_to_anthropic(adaptation: AdaptedRequestBuilder) -> None:
    """Rewrite a response schema to Anthropic's supported subset, as the Anthropic SDK's
    `transform_schema` does: what it cannot enforce moves into descriptions, and objects that
    allowed extra keys are closed. Anthropic has no unenforced schema format, so a schema it
    would still reject (`schema_support.anthropic_rejections`: `{}`, a node emptied by moving
    its keywords, a recursive reference) leaves the format out."""
    fmt = adaptation.request.response_format
    if not isinstance(fmt, ResponseFormatJsonSchema):
        return
    fitted = anthropic_transform(fmt.schema)
    rejections = anthropic_rejections(fitted.schema)
    if rejections:
        adaptation.note(
            "response_format_dropped",
            f"Anthropic cannot take the schema ({'; '.join(rejections)}): the format is left out",
        )
        adaptation.request = replace(adaptation.request, response_format=None)
        return
    if fitted.moved:
        adaptation.note(
            "response_schema_weakened",
            f"not enforced, moved into descriptions: {', '.join(fitted.moved)}",
        )
    if fitted.strengthened:
        adaptation.note(
            "response_schema_strengthened", "objects that allowed extra keys now forbid them"
        )
    if fitted.schema != fmt.schema:
        adaptation.request = replace(
            adaptation.request, response_format=replace(fmt, schema=fitted.schema)
        )


def note_unenforced_response_keywords(
    adaptation: AdaptedRequestBuilder, target: SchemaTarget
) -> None:
    """Record the keywords of a response schema that `target` accepts but does not enforce; the
    schema is sent as given."""
    fmt = adaptation.request.response_format
    if isinstance(fmt, ResponseFormatJsonSchema):
        unsupported = unsupported_keywords(fmt.schema, target)
        if unsupported:
            adaptation.note("response_schema_weakened", f"not enforced: {', '.join(unsupported)}")


def drop_non_object_response_format(adaptation: AdaptedRequestBuilder) -> None:
    """Leave out a response schema whose root is not an object, which OpenAI rejects with or
    without strict mode."""
    fmt = adaptation.request.response_format
    if isinstance(fmt, ResponseFormatJsonSchema) and not is_plain_object_root(fmt.schema):
        adaptation.note(
            "response_format_dropped",
            "OpenAI takes only an object root (type: object): the format is left out",
        )
        adaptation.request = replace(adaptation.request, response_format=None)


def move_rejected_keywords(
    adaptation: AdaptedRequestBuilder, rejects: Callable[[dict[str, Any], str], bool]
) -> None:
    """Move each keyword the target rejects the request over, strict or not
    (`schema_support.gemini_rejects`, `openai_rejects`), into its node's description, in the
    response schema and in every tool's input schema."""
    fmt = adaptation.request.response_format
    if isinstance(fmt, ResponseFormatJsonSchema):
        fitted = move_keywords(fmt.schema, rejects)
        if fitted.moved:
            adaptation.note(
                "response_schema_weakened",
                f"rejected by the provider, moved into descriptions: {', '.join(fitted.moved)}",
            )
            adaptation.request = replace(
                adaptation.request, response_format=replace(fmt, schema=fitted.schema)
            )

    def fit(tool: ToolDefinition) -> ToolDefinition:
        fitted = move_keywords(tool.input_schema, rejects)
        if not fitted.moved:
            return tool
        adaptation.note(
            "tool_schema_weakened",
            f"tool {tool.name}: rejected by the provider, moved into descriptions: "
            f"{', '.join(fitted.moved)}",
        )
        return replace(tool, input_schema=fitted.schema)

    _replace_tools(adaptation, fit)


def move_strict_rejected_keywords(adaptation: AdaptedRequestBuilder) -> None:
    """Fit a strict schema OpenAI's strict mode would reject, the response format's or a strict
    tool's input schema, by moving the keywords that constrain only their own node's value
    (`schema_support.openai_strict_movable`) into descriptions, when that alone makes it
    acceptable. A schema with any other problem is left as given, and the steps after this one
    send it without strict mode (`unenforce_incompatible_response_format`,
    `unenforce_incompatible_strict_tools`)."""

    def fitted(schema: JSONObject) -> TransformedSchema | None:
        if not openai_strict_violations(schema):
            return None
        fit = move_keywords(schema, openai_strict_movable)
        return fit if fit.moved and not openai_strict_violations(fit.schema) else None

    fmt = adaptation.request.response_format
    if isinstance(fmt, ResponseFormatJsonSchema) and fmt.strict:
        fit = fitted(fmt.schema)
        if fit is not None:
            adaptation.note(
                "response_schema_weakened",
                f"not enforced in strict mode, moved into descriptions: {', '.join(fit.moved)}",
            )
            adaptation.request = replace(
                adaptation.request, response_format=replace(fmt, schema=fit.schema)
            )

    def fit_tool(tool: ToolDefinition) -> ToolDefinition:
        fit = fitted(tool.input_schema) if tool.strict is True else None
        if fit is None:
            return tool
        adaptation.note(
            "tool_schema_weakened",
            f"tool {tool.name}: not enforced in strict mode, moved into descriptions: "
            f"{', '.join(fit.moved)}",
        )
        return replace(tool, input_schema=fit.schema)

    _replace_tools(adaptation, fit_tool)


def unenforce_incompatible_response_format(adaptation: AdaptedRequestBuilder) -> None:
    """Send a strict response schema that breaks OpenAI's strict-mode rules without strict mode,
    rather than have the provider reject it."""
    fmt = adaptation.request.response_format
    if not isinstance(fmt, ResponseFormatJsonSchema) or not fmt.strict:
        return
    violations = openai_strict_violations(fmt.schema)
    if violations:
        adaptation.note(
            "response_format_unenforced", f"sent with strict false: {'; '.join(violations)}"
        )
        adaptation.request = replace(adaptation.request, response_format=replace(fmt, strict=False))


# --- tools ----------------------------------------------------------------------------------------


def _replace_tools(
    adaptation: AdaptedRequestBuilder, change: Callable[[ToolDefinition], ToolDefinition]
) -> None:
    tools = tuple(change(tool) for tool in adaptation.request.tools)
    if tools != adaptation.request.tools:
        adaptation.request = replace(adaptation.request, tools=tools)


def resolve_automatic_strictness(adaptation: AdaptedRequestBuilder) -> None:
    """Resolve each tool's `"auto"` strictness, for a dialect without it, to what it meant where
    it was asked: OpenAI's "strict if the schema can be made compatible", so strict when OpenAI's
    strict rules accept the schema and non-strict otherwise. No loss for the resolution itself;
    a target that cannot enforce the resulting strict tool records that in the steps after it
    (`unenforce_incompatible_strict_tools`, `fit_strict_tools_to_gemini`)."""
    _replace_tools(
        adaptation,
        lambda tool: (
            replace(tool, strict=not openai_strict_violations(tool.input_schema))
            if tool.strict == "auto"
            else tool
        ),
    )


def unenforce_incompatible_strict_tools(
    adaptation: AdaptedRequestBuilder, problems: Callable[[JSONObject], tuple[str, ...]]
) -> None:
    """Send a strict tool whose schema the target cannot enforce strictly as non-strict,
    rather than have the provider reject it."""

    def fit(tool: ToolDefinition) -> ToolDefinition:
        if tool.strict is not True:
            return tool
        found = problems(tool.input_schema)
        if not found:
            return tool
        adaptation.note(
            "tool_strict_unenforced", f"tool {tool.name}: sent non-strict: {'; '.join(found)}"
        )
        return replace(tool, strict=False)

    _replace_tools(adaptation, fit)


def fit_strict_tools_to_gemini(adaptation: AdaptedRequestBuilder) -> None:
    """Gemini validates calls for the whole request (`VALIDATED`), and only when the model may
    choose not to call: a required or forced call makes strict tools unenforced, otherwise any
    strict tool makes every tool validated. Keywords Gemini does not enforce, or does not
    validate calls against, are recorded."""
    request = adaptation.request
    strict = [tool for tool in request.tools if tool.strict is True]
    if not strict:
        return
    if not isinstance(request.tool_choice, ToolChoiceAuto | None):
        for tool in strict:
            adaptation.note("tool_strict_unenforced", f"tool {tool.name}: a call is required")
        _replace_tools(adaptation, lambda tool: replace(tool, strict=False))
        return
    for tool in strict:
        unsupported = sorted(
            {
                *unsupported_keywords(tool.input_schema, "gemini"),
                *gemini_unvalidated_keywords(tool.input_schema),
            }
        )
        if unsupported:
            adaptation.note(
                "tool_schema_weakened", f"tool {tool.name}: not enforced: {', '.join(unsupported)}"
            )
    loose = [tool.name for tool in request.tools if tool.strict is not True]
    if loose:
        adaptation.note(
            "tool_strict_strengthened",
            f"calls are validated for the whole request: {', '.join(loose)} too",
        )
        _replace_tools(adaptation, lambda tool: replace(tool, strict=True))


def drop_output_schemas(
    adaptation: AdaptedRequestBuilder,
    code: str,
    why: str,
    *,
    only: frozenset[tuple[str | None, str]] | None = None,
) -> None:
    """Leave out tools' output schemas (every tool's, or those of the tools `only` names by
    namespace and name), recorded under `code` with `why`."""

    def drop(tool: ToolDefinition) -> ToolDefinition:
        if tool.output_schema is None or (
            only is not None and (tool.namespace, tool.name) not in only
        ):
            return tool
        adaptation.note(code, f"tool {tool.name}: {why}")
        return replace(tool, output_schema=None)

    _replace_tools(adaptation, drop)
