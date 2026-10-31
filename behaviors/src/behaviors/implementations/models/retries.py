"""Retrying wrapper around a single model API call."""

from collections.abc import Collection

import anyio

from behaviors.interfaces.model_apis import ModelAPI, StreamingModelAPI
from behaviors.types.failure import FailureCategory, ModelCallFailure
from behaviors.types.response import ModelRequest, ModelResponse
from behaviors.types.retry import RetryConfig
from behaviors.types.streaming import StreamEventCallback, StreamRestart

DEFAULT_FAILURE_CATEGORIES_TO_RETRY: tuple[FailureCategory, ...] = (
    "rate_limited",
    "overloaded",
    "server_error",
)


async def generate_with_retries(
    model_api: ModelAPI,
    request: ModelRequest,
    *,
    retry: RetryConfig | None,
    failure_categories_to_retry: Collection[FailureCategory],
    callback: StreamEventCallback | None = None,
) -> ModelResponse | ModelCallFailure:
    """Call `model_api` once, retrying failures in the configured categories.

    With a `retry` schedule, failures in `failure_categories_to_retry` are
    retried with backoff; once attempts are exhausted the last failure is
    returned as data, like any other failure. With a `callback` and a
    ``StreamingModelAPI``, each attempt streams its events and a
    ``StreamRestart`` is emitted before every retry.
    """
    attempt = 1
    while True:
        if callback is not None and isinstance(model_api, StreamingModelAPI):
            response = await model_api.generate_with_events(request, callback)
        else:
            response = await model_api.generate(request)
        if (
            isinstance(response, ModelResponse)
            or retry is None
            or response.category not in failure_categories_to_retry
        ):
            return response
        if attempt >= retry.max_attempts:
            return response
        await anyio.sleep(retry.backoff_seconds(attempt))
        if callback is not None:
            await callback(StreamRestart())
        attempt += 1
