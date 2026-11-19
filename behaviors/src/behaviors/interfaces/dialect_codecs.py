"""Dialect codec protocols: converting between a provider API dialect and the model vocabulary.

One codec per `Dialect`, in two halves. `ModelSideCodec` is what a `ModelAPI` implementation
needs: adapt a `ModelRequest` to what the dialect can carry, render it, and parse what the
provider sends back.
`HarnessSideCodec` is what a server needs to serve a harness that speaks the dialect:
recognize its requests, parse them into the vocabulary, and render the subject model's answer back
out. `DialectCodec` is both. A dialect may implement only one half: chat completions has a model
side (subject models are served over it) but no harness side (no harness we run speaks it).
Translating between two dialects is the composition of one codec's decode with another's encode
through the vocabulary; no single codec translates.

Codecs are pure: no I/O and no provider SDK imports.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from behaviors.types.adaptation import RequestLoss, ResponseLoss
from behaviors.types.dialect import Dialect, EncodedRequest, Headers
from behaviors.types.failure import ModelCallFailure
from behaviors.types.json import JSONObject
from behaviors.types.provider_tool import ProviderTool
from behaviors.types.response import ModelRequest, ModelResponse, StopReason
from behaviors.types.streaming import StreamEvent, SubjectStreamItem
from behaviors.types.tool import ToolParam
from fxtr.entity_defns.dynamic_data import DynamicData


@dataclass(frozen=True)
class Route:
    """The minimum needed to serve a request: the model it names, whether it streams, and the
    kind of endpoint it targets.

    Extracted tolerantly, never through the full decoder, so a decoder bug cannot block a request
    the provider would accept.

    Attributes:
        model: The model string the request names, or None when it names none (the provider
            will say so).
        streaming: Whether the request asks for a streamed response.
        kind: `completion` for a model call, `count_tokens` for token counting, `other` for any
            other endpoint of the dialect.
    """

    model: str | None
    streaming: bool
    kind: Literal["completion", "count_tokens", "other"]


class CodecError(Exception):
    """A codec could not do its job: it was handed bytes that are not its dialect (a streamed
    frame whose data is not JSON, a response body of the wrong shape, a stream cut off inside a
    frame), or a request whose dialect extras collide with what the codec renders (the decoder
    and encoder disagree about what the codec models: a codec bug).

    Never a provider-reported failure (those are `ModelCallFailure` results). On a normal model
    call it fails the call; a server translating for a harness treats it as a fault of the
    translation.
    """


class RoutingError(Exception):
    """A request a codec recognizes as its dialect but cannot route, with the dialect's answer.

    Attributes:
        status: HTTP status to answer with.
        error_type: Error type in the internal Anthropic-flavoured vocabulary (`not_found_error`,
            `not_implemented`, ...), which the codec's `encode_error` renders in its own shape.
        message: Human-readable explanation.
    """

    def __init__(self, status: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class DecodedRequest:
    """A harness request decoded into the vocabulary.

    Attributes:
        model: The model string the request named. `ModelRequest` has no model field because
            model identity lives on the `ModelAPI`; a server routes on this string.
        request: The decoded request, with any dialect extras attached.
        streaming: Whether the request asked for a streamed response.
        decode_losses: What the decoder left out because the vocabulary has no slot for it, one
            `RequestLoss` per code (the codes are listed on `_common.LossLog`); empty when
            the request was carried whole.
    """

    model: str
    request: ModelRequest
    streaming: bool
    decode_losses: tuple[RequestLoss, ...] = ()


@dataclass(frozen=True)
class AdaptedRequest:
    """A request changed to fit a dialect or model, and one loss per change.

    Attributes:
        request: The request to render.
        losses: What adapting it changed, in the order the changes were made.
    """

    request: ModelRequest
    losses: tuple[RequestLoss, ...] = ()

    def with_change(self, request: ModelRequest, *losses: RequestLoss) -> "AdaptedRequest":
        """This adaptation followed by another change: `request`, with `losses` added."""
        return AdaptedRequest(request=request, losses=(*self.losses, *losses))


class SseDecoder(Protocol):
    """Incremental parser of a dialect's streamed response into stream events and a result."""

    def feed(self, chunk: bytes) -> list[StreamEvent]:
        """Consume the next bytes of the stream; returns the events they completed.

        Raises `CodecError` for a frame whose data is not the dialect's JSON.
        """
        ...

    def finish(self) -> ModelResponse | ModelCallFailure:
        """The assembled result once the stream has ended (an in-stream error is a failure).

        A final frame the stream ended without terminating is decoded into the result (its
        events are not relayed); a stream cut off inside a frame raises `CodecError`.
        """
        ...

    def complete(self) -> bool:
        """Whether the stream's terminal frame has been fed: the provider's final event (or its
        in-stream error), after which nothing more is expected and `finish` returns what the
        provider concluded rather than a stream cut short."""
        ...

    def raw_message(self) -> JSONObject | None:
        """The provider's final message as the raw frames describe it.

        For comparison against a re-encoding of the decoded result. None before `finish`, and
        None when the stream ended in an error.
        """
        ...


@runtime_checkable
class ModelSideCodec(Protocol):
    """The half of a codec a `ModelAPI` implementation needs."""

    @property
    def dialect(self) -> Dialect: ...

    def adapt_request(self, model: str, request: ModelRequest) -> AdaptedRequest:
        """`request` changed so that this dialect can carry it, for `model`, with one
        `RequestLoss` per change (`behaviors.implementations.dialects.adaptation` has the
        building blocks). A request this dialect carries whole comes back unchanged, with no
        losses."""
        ...

    def encode_request(
        self, model: str, request: ModelRequest, *, streaming: bool
    ) -> EncodedRequest:
        """Render a request into the dialect, for `model`, exactly as it is: a request is
        adapted first (`adapt_request`), and an encoder does not drop or replace what it cannot
        carry."""
        ...

    def sse_decoder(self) -> SseDecoder:
        """A fresh decoder for one streamed response."""
        ...

    def decode_response(self, body: bytes) -> ModelResponse | ModelCallFailure:
        """Parse a complete, non-streamed response body (`CodecError` if it is not one)."""
        ...

    def decode_failure(self, status: int, body: bytes) -> ModelCallFailure:
        """Classify a non-success response by its status and error body."""
        ...

    def accepts_reasoning_state(self, state: DynamicData, model: str) -> bool:
        """Whether a request to `model` may carry `state` (a `ContentReasoning.reasoning_state`):
        a state of this dialect's provider that `model` can use. The codec's `adapt_request`
        removes the blocks carrying any other state (`adaptation.drop_foreign_reasoning`), and
        its `encode_request` raises on one. Read a state with
        `behaviors.implementations.dialects.codec_utils.own_reasoning_state`: a state that came
        back through a harness may carry this codec's tag without decoding as its type."""
        ...


@dataclass(frozen=True)
class StopRendering:
    """How a harness's dialect renders a response's stop reason (`HarnessSideCodec.render_stop`).

    Attributes:
        losses: What the rendered response says other than as given, one `ResponseLoss` per
            change (a refusal's completeness the dialect cannot state, say).
        as_error: The dialect has no form for the stop that a harness would not read as a
            finished answer (Gemini's `MALFORMED_FUNCTION_CALL` to an Anthropic harness), so a
            server answers the call as a `server_error` failure; the recorded response is the
            model's own. `losses` then says so.
    """

    losses: tuple[ResponseLoss, ...] = ()
    as_error: bool = False


@runtime_checkable
class HarnessSideCodec(Protocol):
    """The half of a codec a server needs to serve a harness speaking the dialect.

    The three `matches_*` methods are detection tiers a server runs across every registered
    codec in order, path first (the reliable signal for fixed endpoints), then a
    provider-identifying header, then a light body-shape check; a codec answers each for its own
    dialect only.
    """

    @property
    def dialect(self) -> Dialect: ...

    @property
    def reasoning_round_trip(self) -> bool:
        """Whether the dialect can carry a subject model's reasoning back to the harness for
        echoing on continuations."""
        ...

    def matches_path(self, target: str) -> bool:
        """Whether `target` (path plus query) is one of the dialect's endpoints."""
        ...

    def matches_headers(self, headers: Headers) -> bool:
        """Whether a header identifies the request as this dialect (e.g. `anthropic-version`)."""
        ...

    def matches_body(self, body: JSONObject) -> bool:
        """Whether the body's shape identifies the request as this dialect."""
        ...

    def route(self, target: str, body: JSONObject) -> Route:
        """Read what is needed to serve the request, without decoding it.

        Raises `RoutingError` for a request of this dialect that cannot be served.
        """
        ...

    def decode_request(self, target: str, headers: Headers, body: bytes) -> DecodedRequest:
        """Parse a harness request into the vocabulary, attaching dialect extras."""
        ...

    def provider_tools(self, body: JSONObject) -> tuple[ProviderTool, ...]:
        """The provider-side tools the request declares (`behaviors.types.provider_tool`).

        Read tolerantly from the parsed body, never through the decoder, so that a decoder bug
        cannot hide a tool from a server's policy check, and from every place the dialect
        accepts a declaration (a mid-conversation addition counts as much as the tool list).
        A tool the codec cannot place, by type or by where it executes, is reported with family
        `unknown`.
        """
        ...

    def strip_reasoning(self, request: ModelRequest) -> ModelRequest:
        """The request without the harness's reasoning configuration: `sampling.reasoning`
        cleared and the extras leaves that belong to that configuration dropped, so no fragment
        of it (a leftover `thinking.display`, say) reaches a provider on its own."""
        ...

    def encode_sse(
        self,
        model: str,
        items: AsyncIterator[SubjectStreamItem],
        *,
        tools: tuple[ToolParam, ...] = (),
    ) -> AsyncIterator[bytes]:
        """Render a subject model's stream as the dialect's streaming wire format.

        `tools` are the harness request's tool declarations, for a dialect whose rendering of a
        call depends on how the tool was declared (Responses: a freeform tool's call is a
        `custom_tool_call`, a grouped tool's call carries its namespace).

        Each yielded chunk is whole SSE events, so another event may be sent between chunks.
        """
        ...

    def keepalive_frame(self) -> bytes | None:
        """One SSE event a streaming client of the dialect accepts and ignores, sent while a
        translated stream has nothing to render, so the client's idle timeout sees a live stream;
        None for a dialect with no such event."""
        ...

    def encode_response(
        self, model: str, response: ModelResponse, *, tools: tuple[ToolParam, ...] = ()
    ) -> JSONObject:
        """Render an assembled response as the dialect's non-streaming body (`tools` as for
        `encode_sse`)."""
        ...

    def encode_failure(self, failure: ModelCallFailure) -> tuple[int, bytes]:
        """Render a request-time failure as the dialect's (HTTP status, error body)."""
        ...

    def render_stop(self, stop: StopReason, *, streaming: bool) -> StopRendering:
        """How `encode_sse` (`streaming`) or `encode_response` renders a response ending in
        `stop`: what it says other than as given, and whether the call must be answered as an
        error instead. The encoders take their wire stop fields from the same per-dialect
        mapping, so the two cannot disagree."""
        ...

    def encode_error(self, status: int, error_type: str, message: str) -> bytes:
        """Render an error a server synthesizes itself (unknown endpoint, unparsable body, ...)
        in the dialect's error shape."""
        ...


@runtime_checkable
class DialectCodec(HarnessSideCodec, ModelSideCodec, Protocol):
    """A full codec: both halves of one dialect."""
