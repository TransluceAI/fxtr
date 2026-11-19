"""Pure pieces shared by the two OpenAI dialect codecs (chat completions and Responses).

Both surfaces report errors the same two ways, an HTTP status with an error body or an error
object delivered inside a 200 response, and both take the same reasoning-effort vocabulary and the
same response formats (flat in Responses `text.format`, with the schema's fields nested under
`json_schema` in chat completions `response_format`), so the mappings live here.
"""

import copy
import dataclasses
import json
from typing import Any, cast

from behaviors.implementations.dialects.adaptation import AdaptedRequestBuilder, replace_reasoning
from behaviors.types.failure import FailureCategory, ModelCallFailure
from behaviors.types.json import JSONObject
from behaviors.types.response import (
    ReasoningParams,
    ResponseFormat,
    ResponseFormatJsonObject,
    ResponseFormatJsonSchema,
    ResponseFormatText,
    SpeedTier,
)

MAX_FAILURE_DESCRIPTION = 500

_CONTEXT_LENGTH_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "request too large",
    "context length",
    "exceeds the maximum number of tokens",  # Gemini: "The input token count (N) exceeds ..."
)
_CONTENT_FILTER_MARKERS = (
    "content_policy_violation",
    "content_filter",
    "invalid_prompt",
    "content management policy",
)
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "insufficient_quota",
    "credit_balance_exhausted",
)


def category_for_status(status: int | None, haystack: str) -> FailureCategory:
    """The failure category for an OpenAI-shaped error, from its HTTP status and error text."""
    lower = haystack.lower()
    if status == 400:
        if any(marker in lower for marker in _CONTEXT_LENGTH_MARKERS):
            return "context_length"
        if any(marker in lower for marker in _CONTENT_FILTER_MARKERS):
            return "content_filter"
        return "invalid_request"
    if status in (401, 403):
        return "authentication"
    if status == 429:
        return "rate_limited"
    # 408 and the gateway statuses are how OpenRouter reports a provider that
    # is temporarily unavailable; 529 is Anthropic's overload status, passed
    # through by its OpenAI-compatible endpoint.
    if status in (408, 502, 503, 504, 529):
        return "overloaded"
    if status is not None and 400 <= status < 500:
        return "invalid_request"
    return "server_error"


_INVALID_REQUEST_MARKERS = ("invalid_image", "invalid_file", "invalid_request")


def category_for_statusless_error(haystack: str) -> FailureCategory:
    """The category for an error delivered with no HTTP status (inside an already-200 stream),
    classified by its text alone."""
    lower = haystack.lower()
    if any(marker in lower for marker in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    if any(marker in lower for marker in _CONTEXT_LENGTH_MARKERS):
        return "context_length"
    if any(marker in lower for marker in _CONTENT_FILTER_MARKERS):
        return "content_filter"
    if any(marker in lower for marker in _INVALID_REQUEST_MARKERS):
        return "invalid_request"
    return "server_error"


def failure_from_error_object(
    error: dict[str, Any], *, http_status: int | None = None
) -> ModelCallFailure:
    """An OpenAI-shaped error object (`message`, `code`, `type`) as a failure.

    `http_status` is the status of the non-success response the object arrived on. An object
    delivered inside a 200 (an error event, an error chunk) has none; OpenRouter then relays the
    upstream provider's status as a numeric `code`, which stands in for it. With no status at
    all, the text (message, code and type together) decides the category.
    """
    code = error.get("code")
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    relayed = code if isinstance(code, int) and not isinstance(code, bool) else None
    status = http_status if http_status is not None else relayed
    message = str(error.get("message") or "") or json.dumps(error)
    haystack = " ".join(
        part for part in (message, error.get("code"), error.get("type")) if isinstance(part, str)
    )
    category = (
        category_for_status(status, haystack)
        if status is not None
        else category_for_statusless_error(haystack)
    )
    return ModelCallFailure(
        category=category,
        description=message[:MAX_FAILURE_DESCRIPTION],
        provider_http_status=status,
    )


def decode_failure_body(status: int, body: bytes) -> ModelCallFailure:
    """A non-success response as a failure: from its error object when the body carries one, from
    the body text otherwise (gateways answer with HTML)."""
    text = body.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    error = cast("dict[str, Any]", parsed).get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        return failure_from_error_object(cast("dict[str, Any]", error), http_status=status)
    return ModelCallFailure(
        category=category_for_status(status, text),
        description=text[:MAX_FAILURE_DESCRIPTION],
        provider_http_status=status,
    )


# budget_tokens → reasoning effort cut points: budgets at or under each bound map to its level.
_BUDGET_EFFORT_BOUNDS: tuple[tuple[int, str], ...] = ((4096, "low"), (16384, "medium"))


def reasoning_effort(reasoning: ReasoningParams | None) -> str | None:
    """The OpenAI reasoning-effort value for a neutral reasoning config, or None to omit it.

    The effort level when one is given, passed through verbatim (OpenAI's 5.6 family and OpenRouter
    both accept ``max``; a model whose ceiling is lower — e.g. GPT 5.5 tops out at ``xhigh`` —
    validates the value server-side, so the caller pins a level the model accepts). A level derived
    from ``budget_tokens`` otherwise (OpenAI has no budget parameter), ``none`` when reasoning is
    explicitly disabled, and None — no value at all, the provider's default — when the config only
    says "enabled". Whether the model accepts the value is the provider's to validate.
    """
    if reasoning is None:
        return None
    if not reasoning.enabled:
        return "none"
    if reasoning.effort is not None:
        return reasoning.effort
    if reasoning.budget_tokens is not None:
        for bound, effort in _BUDGET_EFFORT_BOUNDS:
            if reasoning.budget_tokens <= bound:
                return effort
        return "high"
    return None


def service_tier_param(speed: SpeedTier) -> str:
    """The `service_tier` value for a neutral speed tier: `fast` (OpenAI's and OpenRouter's
    premium tier, whose older name is `priority`) or `default`."""
    return "fast" if speed == "fast" else "default"


def service_tier_speed(value: object) -> SpeedTier | None:
    """The neutral speed tier a `service_tier` value names: `fast`, its older name `priority`
    and `ultrafast` are `fast`; `default` is `standard`; a tier the vocabulary does not model
    (`flex`, `scale`, `auto`), or no value, is None."""
    if value in ("fast", "priority", "ultrafast"):
        return "fast"
    if value == "default":
        return "standard"
    return None


def budget_as_effort(adaptation: AdaptedRequestBuilder) -> None:
    """Send a reasoning budget given without an effort level as the effort level whose typical
    spend is nearest (`reasoning_effort`): the OpenAI dialects have no budget parameter."""
    reasoning = adaptation.request.sampling.reasoning
    if (
        reasoning is None
        or not reasoning.enabled
        or reasoning.effort is not None
        or reasoning.budget_tokens is None
    ):
        return
    effort = reasoning_effort(reasoning)
    replace_reasoning(
        adaptation,
        dataclasses.replace(reasoning, effort=cast("Any", effort), budget_tokens=None),
        "reasoning_budget",
        f"a budget of {reasoning.budget_tokens} tokens sent as effort {effort}",
    )


DEFAULT_SCHEMA_NAME = "response"
"""The name a JSON-schema response format is sent under when it has none (OpenAI requires one)."""

_JSON_SCHEMA_FIELDS = frozenset({"name", "schema", "description", "strict"})


def response_format_from_flat(fmt: dict[str, Any]) -> ResponseFormat | None:
    """A response format in the flat Responses shape (`{"type": "json_schema", "name", "schema",
    ...}`), or None for one the vocabulary cannot carry whole (an unknown type or field), which
    the caller leaves in the extras. An absent `strict` is OpenAI's default, false."""
    kind = fmt.get("type")
    rest = set(fmt) - {"type"}
    if kind == "text" and not rest:
        return ResponseFormatText()
    if kind == "json_object" and not rest:
        return ResponseFormatJsonObject()
    if kind != "json_schema" or not rest <= _JSON_SCHEMA_FIELDS:
        return None
    schema, name = fmt.get("schema"), fmt.get("name")
    description, strict = fmt.get("description"), fmt.get("strict")
    if (
        not isinstance(schema, dict)
        or not (name is None or isinstance(name, str))
        or not (description is None or isinstance(description, str))
        or not (strict is None or isinstance(strict, bool))
    ):
        return None
    return ResponseFormatJsonSchema(
        schema=cast("JSONObject", schema),
        strict=bool(strict),
        name=name,
        description=description,
    )


def response_format_flat(response_format: ResponseFormat) -> dict[str, Any]:
    """A response format in the flat Responses shape (the inverse of
    `response_format_from_flat`). `strict` is always written, so the request says whether the
    schema is enforced."""
    if isinstance(response_format, ResponseFormatText):
        return {"type": "text"}
    if isinstance(response_format, ResponseFormatJsonObject):
        return {"type": "json_object"}
    out: dict[str, Any] = {
        "type": "json_schema",
        "name": response_format.name or DEFAULT_SCHEMA_NAME,
        "schema": copy.deepcopy(dict(response_format.schema)),
        "strict": response_format.strict,
    }
    if response_format.description is not None:
        out["description"] = response_format.description
    return out
