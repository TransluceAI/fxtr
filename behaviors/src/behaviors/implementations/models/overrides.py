"""A wrapper that pins sampling parameters across every request to a ModelAPI.

The ModelAPI implementations only pin the model ID itself, and pass through all other parameters.
However, it may be useful to pin other parameters across every request, such as reasoning effort or
temperature. For example, if you want to test Claude at high effort in an agent harness that does
not support setting reasoning effort, you can use this wrapper to pin the effort to high for every
request.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import cast

from behaviors.interfaces.model_apis import StreamingModelAPI
from behaviors.types.failure import ModelCallFailure
from behaviors.types.response import ModelRequest, ModelResponse, ReasoningParams
from behaviors.types.streaming import StreamEventCallback


@dataclass(frozen=True)
class SamplingOverrides:
    """A ``StreamingModelAPI`` that overrides sampling fields, then delegates.

    Attributes:
        inner: The model API serving the rewritten requests.
        reasoning: Reasoning config to pin on every request, or None to leave
            requests untouched.
    """

    inner: StreamingModelAPI
    reasoning: ReasoningParams | None = None

    def _apply(self, request: ModelRequest) -> ModelRequest:
        if self.reasoning is None:
            return request
        return replace(request, sampling=replace(request.sampling, reasoning=self.reasoning))

    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        return await self.inner.generate(self._apply(request))

    async def generate_with_events(
        self, request: ModelRequest, callback: StreamEventCallback
    ) -> ModelResponse | ModelCallFailure:
        return await self.inner.generate_with_events(self._apply(request), callback)

    async def aclose(self) -> None:
        """Release the wrapped API's client, if it holds one."""
        aclose = getattr(self.inner, "aclose", None)
        if callable(aclose):
            await cast("Callable[[], Awaitable[None]]", aclose)()
