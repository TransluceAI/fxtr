"""Wrappers that change every request to a ModelAPI before delegating to it.

The ModelAPI implementations only pin the model ID itself, and pass through all other parameters.
However, it may be useful to pin other parameters across every request, such as reasoning effort or
temperature. For example, if you want to test Claude at high effort in an agent harness that does
not support setting reasoning effort, `SamplingOverrides` pins the effort to high for every
request. `FinalBlockCacheControl` puts a cache breakpoint on every request's final block, so each
turn of a conversation reads the prefix the previous turn wrote.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import cast

from behaviors.interfaces.model_apis import StreamingModelAPI
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import CacheableContentText, ContentText
from behaviors.types.failure import ModelCallFailure
from behaviors.types.message import CacheableToolMessage, ToolMessage
from behaviors.types.response import ModelRequest, ModelResponse, ReasoningParams, SpeedTier
from behaviors.types.streaming import StreamEventCallback


@dataclass(frozen=True)
class SamplingOverrides:
    """A ``StreamingModelAPI`` that overrides sampling fields, then delegates.

    Attributes:
        inner: The model API serving the rewritten requests.
        reasoning: Reasoning config to pin on every request, or None to leave
            requests untouched.
        speed: Speed tier to pin on every request, or None to leave requests
            untouched.
    """

    inner: StreamingModelAPI
    reasoning: ReasoningParams | None = None
    speed: SpeedTier | None = None

    def _apply(self, request: ModelRequest) -> ModelRequest:
        sampling = request.sampling
        if self.reasoning is not None:
            sampling = replace(sampling, reasoning=self.reasoning)
        if self.speed is not None:
            sampling = replace(sampling, speed=self.speed)
        if sampling is request.sampling:
            return request
        return replace(request, sampling=sampling)

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


def with_final_block_breakpoint(request: ModelRequest, cache_control: CacheControl) -> ModelRequest:
    """`request` with `cache_control` on its final block, if that block can take one and the
    caller left it unmarked: a non-empty text block (as `CacheableContentText`) or a tool result
    (as `CacheableToolMessage`). Otherwise `request` unchanged.

    Whether the breakpoint reaches the provider, and with which TTL, is the dialect's: the codec's
    `adapt_request` keeps what its provider accepts and records what it changes (Anthropic, for
    one, keeps the latest four breakpoints and no longer TTL after a shorter one).
    """
    if not request.context:
        return request
    final = request.context[-1]
    if isinstance(final, ToolMessage):
        if isinstance(final, CacheableToolMessage) and final.cache_control is not None:
            return request
        marked: object = CacheableToolMessage(
            content=final.content,
            tool_call_id=final.tool_call_id,
            is_error=final.is_error,
            cache_control=cache_control,
        )
    else:
        if not final.content:
            return request
        block = final.content[-1]
        if (
            not isinstance(block, ContentText)
            or not block.text
            or (isinstance(block, CacheableContentText) and block.cache_control is not None)
        ):
            return request
        content = (
            *final.content[:-1],
            CacheableContentText(text=block.text, cache_control=cache_control),
        )
        marked = replace(final, content=content)
    return replace(request, context=(*request.context[:-1], marked))


@dataclass(frozen=True)
class FinalBlockCacheControl:
    """A ``StreamingModelAPI`` that puts a cache breakpoint on every request's final block (see
    `with_final_block_breakpoint`), then delegates.

    Attributes:
        inner: The model API serving the marked requests.
        cache_control: The breakpoint to add.
    """

    inner: StreamingModelAPI
    cache_control: CacheControl = CacheControl()

    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        return await self.inner.generate(with_final_block_breakpoint(request, self.cache_control))

    async def generate_with_events(
        self, request: ModelRequest, callback: StreamEventCallback
    ) -> ModelResponse | ModelCallFailure:
        return await self.inner.generate_with_events(
            with_final_block_breakpoint(request, self.cache_control), callback
        )

    async def aclose(self) -> None:
        """Release the wrapped API's client, if it holds one."""
        aclose = getattr(self.inner, "aclose", None)
        if callable(aclose):
            await cast("Callable[[], Awaitable[None]]", aclose)()
