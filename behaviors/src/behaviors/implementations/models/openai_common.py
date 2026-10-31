"""Failure classification shared by the OpenAI-backed ``ModelAPI`` implementations.

Both OpenAI surfaces (Chat Completions in ``openai_compatible``, Responses in
``openai_responses``) speak through the same SDK and report errors the same
two ways — an ``openai``/``httpx2`` exception, or an error object delivered
inside a 200 response — so the mapping to ``ModelCallFailure`` lives here.
"""

from typing import Any

import httpx2
import openai

from behaviors.implementations.models._client_misuse import raise_if_client_misused
from behaviors.types.failure import FailureCategory, ModelCallFailure
from behaviors.types.response import ReasoningParams

MAX_FAILURE_DESCRIPTION = 500

_CONTEXT_LENGTH_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "request too large",
    "context length",
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


class ProviderReportedError(Exception):
    """An error object the provider reported inside a 200 response.

    OpenRouter (and some compatible servers) return HTTP 200 with an ``error``
    object in the body or in a streamed chunk instead of a non-200 status; the
    Responses API streams a terminal ``response.failed`` the same way.
    """

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


def category_for_status(status: int | None, haystack: str) -> FailureCategory:
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


def failure_from_exception(
    exc: openai.APIError | httpx2.HTTPError | ProviderReportedError,
) -> ModelCallFailure:
    description = str(exc)[:MAX_FAILURE_DESCRIPTION]
    if isinstance(exc, ProviderReportedError):
        return ModelCallFailure(
            category=category_for_status(exc.status, description),
            description=description,
            provider_http_status=exc.status,
        )
    if isinstance(exc, openai.APIStatusError):
        haystack = description
        if isinstance(exc.code, str):
            haystack += " " + exc.code
        return ModelCallFailure(
            category=category_for_status(exc.status_code, haystack),
            description=description,
            provider_http_status=exc.status_code,
        )
    if isinstance(exc, openai.APIError) and not isinstance(exc, openai.APIConnectionError):
        # An error object delivered inside an already-200 stream (the Responses API
        # reports quota exhaustion this way): no HTTP status, so classify by content.
        lower = (description + " " + (exc.code or "")).lower()
        category: FailureCategory = "server_error"
        if any(marker in lower for marker in _RATE_LIMIT_MARKERS):
            category = "rate_limited"
        elif any(marker in lower for marker in _CONTEXT_LENGTH_MARKERS):
            category = "context_length"
        elif any(marker in lower for marker in _CONTENT_FILTER_MARKERS):
            category = "content_filter"
        return ModelCallFailure(
            category=category, description=description, provider_http_status=None
        )
    if isinstance(exc, openai.APIConnectionError):
        raise_if_client_misused(exc)
    # Connection failures and timeouts (openai.APIConnectionError, raw httpx2
    # transport errors escaping the SDK mid-stream): the provider is
    # unreachable or unresponsive, which consumers treat like an overload.
    return ModelCallFailure(
        category="overloaded", description=description, provider_http_status=None
    )


# budget_tokens → reasoning effort cut points: budgets at or under each bound map to its level.
_BUDGET_EFFORT_BOUNDS: tuple[tuple[int, str], ...] = ((4096, "low"), (16384, "medium"))


def reasoning_effort(reasoning: ReasoningParams | None) -> str | None:
    """The OpenAI reasoning-effort value for a neutral reasoning config, or None to omit it.

    The effort level verbatim when one is given (including ``xhigh`` and
    ``max``), a level derived from ``budget_tokens`` otherwise (OpenAI has no
    budget parameter), ``none`` when reasoning is explicitly disabled, and
    None — no value at all, the provider's default — when the config only says
    "enabled". Whether the model accepts the value is the provider's to
    validate.
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


def error_status_and_message(error: dict[str, Any]) -> tuple[int | None, str]:
    """The (HTTP status, message) carried by a provider-reported error object."""
    code = error.get("code")
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    status = code if isinstance(code, int) and not isinstance(code, bool) else None
    return status, str(error.get("message") or error)
