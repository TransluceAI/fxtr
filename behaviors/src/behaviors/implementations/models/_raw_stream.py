"""Sending codec-rendered and forwarded requests through a provider SDK client, and decoding the
raw streamed response.

The two SDKs (anthropic, openai) are Stainless-generated and share the same base client. The SDK
stays in charge of everything about the request that is the provider account's business:
credentials, including the dynamic kinds attached per request (a callable API key, an Azure AD
token), the base URL, its default headers, the configured timeout, and the httpx2 client with its
proxies, certificates or (in tests) mock transport. What this module takes over is the send and
the response: the request is built with the SDK's own preparation steps and sent with its httpx2
client directly, so that the response is owned here for every status, streamed. Going through
the SDK's `post` instead would have it parse the stream into typed events, retry on its own
(retry policy belongs to the caller, see `generate_with_retries`), and read a non-success body
before raising, leaving nothing to close if that read were interrupted.
"""

import dataclasses
import logging
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

import anyio
import httpx2

from behaviors.implementations.dialects._common import (
    forwardable_headers,
    forwardable_query,
    split_origin_form_target,
)
from behaviors.implementations.dialects.codec_utils import decode_json_object
from behaviors.implementations.models._workarounds import (
    Prepared,
    RejectionWorkaround,
    WorkaroundLedger,
    fingerprint,
)
from behaviors.interfaces.dialect_codecs import (
    CodecError,
    ModelSideCodec,
    SseDecoder,
)
from behaviors.interfaces.model_apis import ModelError
from behaviors.types.adaptation import RequestLoss
from behaviors.types.dialect import EncodedRequest, Headers, RawHttpResponse
from behaviors.types.failure import ModelCallFailure
from behaviors.types.json import JSONObject
from behaviors.types.response import ModelRequest, ModelResponse
from behaviors.types.streaming import (
    StreamEvent,
    StreamEventCallback,
    StreamKeepalive,
    StreamRestart,
)

MAX_FAILURE_DESCRIPTION = 500
# How long releasing an upstream response may take once its read has been cut short.
_CLOSE_TIMEOUT_S = 10.0


class SdkClient(Protocol):
    """The slice of the anthropic/openai base client the transport uses.

    The underscored members are private to the SDK but unchanged from the httpx-based majors to the
    httpx2-based ones (anthropic 1.x, openai 3.x). `_prepare_options` applies per-request options
    and credentials resolved per request (an OpenAI key given as a callable, an Azure AD token),
    `_build_request` joins the base URL, merges the default headers (static keys among them, with
    the default query as params) and applies the configured timeout, and `_client` is the SDK's
    httpx2 client, which the transport sends with itself. The SDK's send-time steps do not run:
    `_prepare_request` and `custom_auth`, so a client relying on them is refused
    (`require_supported_client`), and the OpenAI client's retry after refreshing a rejected key.
    """

    @property
    def default_headers(self) -> dict[str, Any]: ...

    @property
    def _client(self) -> httpx2.AsyncClient: ...

    def _prepare_options(self, options: Any) -> Awaitable[Any]: ...

    def _build_request(self, options: Any) -> httpx2.Request: ...


def require_supported_client(client: Any, sdk_client_type: type) -> None:
    """Refuse an SDK client whose requests need SDK steps the transport does not run.

    The transport builds a request the SDK's way but sends it itself (`SdkClient`), so the SDK's
    `custom_auth` and `_prepare_request` never run. A client that relies on either would send
    its requests unauthenticated or unsigned: one with an Anthropic credential provider or OpenAI
    workload identity (`custom_auth`), or a Bedrock or Vertex client (`_prepare_request`).
    Credentials applied while building the request are kept: static keys, an OpenAI key given as
    a callable, and an Azure AD token provider.

    Raises:
        ValueError: For a client the transport cannot send for.
    """
    name = type(client).__name__
    if client.custom_auth is not None:
        raise ValueError(
            f"{name} authenticates at send time (a credential provider or workload identity), "
            "which behaviors model APIs do not support; pass a client with an API key"
        )
    if type(client)._prepare_request is not sdk_client_type._prepare_request:  # pyright: ignore[reportPrivateUsage]
        raise ValueError(
            f"{name} changes requests at send time (as Bedrock and Vertex clients do), which "
            f"behaviors model APIs do not support; pass an {sdk_client_type.__name__}"
        )


class RequestOptionsType(Protocol):
    """The SDK's `FinalRequestOptions` class (each SDK has its own), built without validation
    the way the SDK itself does."""

    def construct(self, **fields: Any) -> Any: ...


async def _send(
    client: SdkClient,
    options_type: RequestOptionsType,
    *,
    path: str,
    query: str,
    headers: dict[str, str],
    body: JSONObject,
) -> httpx2.Response:
    """Build a POST the SDK's way and send it with the SDK's httpx2 client, streaming.

    `path` is SDK-relative (SDKs whose base URL already carries the API version take the path
    without it). `query` is appended to the URL verbatim, pairs in order and repeats intact,
    after whatever default query the SDK put there itself; the SDK's own query serializer is not
    used for it because its array formats differ per SDK (`a,b` for one, `name[]=` for the
    other). httpx2 errors propagate for the caller to classify.
    """
    options = options_type.construct(
        method="post", url=path, json_data=body, headers=headers, max_retries=0
    )
    options = await client._prepare_options(options)  # pyright: ignore[reportPrivateUsage]
    request = client._build_request(options)  # pyright: ignore[reportPrivateUsage]
    if query:
        raw_query = query.encode("latin-1")
        if request.url.query:
            raw_query = request.url.query + b"&" + raw_query
        request = httpx2.Request(
            request.method,
            request.url.copy_with(query=raw_query),
            headers=request.headers,
            stream=request.stream,
            extensions=request.extensions,
        )
    try:
        return await client._client.send(request, stream=True)  # pyright: ignore[reportPrivateUsage]
    except RuntimeError as exc:
        # A client used after it was closed, or from an event loop other than the one that
        # opened its connections, fails with a RuntimeError ("Event loop is closed", "... the
        # client has been closed"). That is a caller bug, not an unreachable provider.
        raise ModelError(
            f"SDK transport failed with RuntimeError ({exc}); a client was probably used after "
            "it was closed, or outside the event loop it belongs to"
        ) from exc


class _UpstreamBody:
    """A streamed upstream response's body, and a close that completes even after a cancelled
    read.

    httpx2 closes a response inside its byte iterator when the read stops early, marking it
    closed before it awaits the underlying stream's close; when the read stopped because it was
    cancelled, that await is cut off, and every later `aclose` sees the mark and does nothing,
    leaving the upstream request open. So a read that did not reach the end is closed again at the
    underlying stream, shielded and bounded.
    """

    def __init__(self, response: httpx2.Response) -> None:
        self._response = response
        self._read_to_end = False

    async def chunks(self) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_bytes():
            yield chunk
        self._read_to_end = True

    async def aclose(self) -> None:
        response = self._response
        with anyio.move_on_after(_CLOSE_TIMEOUT_S, shield=True):
            if not response.is_closed:
                await response.aclose()
            elif not self._read_to_end and isinstance(response.stream, httpx2.AsyncByteStream):
                await response.stream.aclose()


def _spelling(client: SdkClient) -> dict[str, str]:
    return {name.lower(): name for name in client.default_headers}


def _joined(headers: Headers, spelling: dict[str, str]) -> dict[str, str]:
    """Header pairs as one mapping, names in the SDK's spelling (so a header the SDK also sets
    replaces the SDK's rather than being joined with it by httpx2) and a repeated name sent once
    with its values joined by ", " (RFC 9110 §5.3: equivalent for list-valued fields, which is
    what these headers are; `anthropic-beta` flags are one example)."""
    out: dict[str, str] = {}
    for name, value in headers:
        key = spelling.get(name.lower(), name)
        out[key] = f"{out[key]}, {value}" if key in out else value
    return out


async def post_raw(
    client: SdkClient,
    encoded: EncodedRequest,
    *,
    path: str,
    options_type: RequestOptionsType,
) -> httpx2.Response:
    """POST a codec-rendered request and return the streamed raw response, whatever its status."""
    return await _send(
        client,
        options_type,
        path=path,
        query=urllib.parse.urlencode(list(encoded.query)),
        headers=_joined(encoded.headers, _spelling(client)),
        body=encoded.body,
    )


def _codec_step[T](stage: str, thunk: Callable[[], T]) -> T:
    """A codec call on the model side. Whatever it raises is a codec failure and surfaces as
    `CodecError`, so a caller can tell a codec that could not do its job (load-bearing for a server
    translating for a harness) from a provider that failed (a `ModelCallFailure` result)."""
    try:
        return thunk()
    except CodecError:
        raise
    except Exception as exc:
        raise CodecError(f"{stage} raised {type(exc).__name__}: {exc}") from exc


_logger = logging.getLogger(__name__)


async def call_and_decode(
    client: SdkClient,
    *,
    codec: ModelSideCodec,
    model: str,
    request: ModelRequest,
    options_type: RequestOptionsType,
    callback: StreamEventCallback,
    relative_path: Callable[[str], str] = lambda path: path,
    workarounds: WorkaroundLedger | None = None,
) -> ModelResponse | ModelCallFailure:
    """One model call: adapt `request` and render it with the codec, send it, classify a non-200
    with the codec, decode a 200 stream through it. A transport failure before any response is
    an `overloaded` result; after the stream has started it ends the stream as a failure result
    too (see `decode_stream`). Every codec call is a `_codec_step`.

    Each attempt is prepared whole before anything is decided: the codec's `adapt_request`, the
    model API's applicable `workarounds` (`_workarounds`), rendering, and the workarounds' wire
    changes. The request's `adaptation_policy` then sees every change: under `refuse`, an attempt
    that would change the request's meaning is not sent, and the result is a locally made
    `adaptation_refused` failure proposing the changes it needed. The result carries the changes
    of the attempt that produced it (`adaptation_losses`).

    A provider failure that a declared workaround recognizes, and that the attempt did not
    already apply, is retried once with it, each workaround at most once per call; the
    workaround is remembered as provisional for this request on recognition, before the retry,
    and confirmed when an attempt with it succeeds. If events of the failed attempt already
    reached `callback`, a `StreamRestart` precedes the retry. A workaround the policy refuses
    leaves the provider's rejection as the result, as an `adaptation_refused` failure proposing
    the refused change.
    """
    policy = request.adaptation_policy
    ledger = workarounds or WorkaroundLedger(())
    request_fingerprint = fingerprint(request)
    active = list(ledger.applicable(request_fingerprint))
    tried: set[str] = set()
    delivered = False

    async def relay(event: StreamEvent) -> None:
        nonlocal delivered
        delivered = True
        await callback(event)

    def encode(adapted_request: ModelRequest) -> EncodedRequest:
        return _codec_step(
            "encode_request", lambda: codec.encode_request(model, adapted_request, streaming=True)
        )

    def prepare(workarounds_to_apply: list[RejectionWorkaround]) -> _Attempt:
        """The attempt with `workarounds_to_apply`. A workaround counts as applied only when it
        changes what is sent: one that leaves the rendered request as it was (no unsigned call to
        sign, a setting `SamplingParams.extra` overrides) records no loss and is *idle*. A
        provisional workaround that would change the request's meaning is kept out under
        `refuse`: it is unverified."""
        adapted = _codec_step("adapt_request", lambda: codec.adapt_request(model, request))
        encoded = encode(adapted.request)
        attempt = _Attempt()
        for workaround in workarounds_to_apply:
            if workaround.apply_to_request is None:
                attempt.applied.append(workaround)
                continue
            changed = workaround.apply_to_request(adapted)
            changed_encoding = encode(changed.request)
            if changed_encoding == encoded:
                attempt.idle.append(workaround)
                continue
            added = changed.losses[len(adapted.losses) :]
            if policy.refuses(added) is not None and not ledger.is_confirmed(workaround):
                attempt.kept_out = (*attempt.kept_out, *added)
                continue
            adapted, encoded = changed, changed_encoding
            attempt.applied.append(workaround)
        wire_losses: list[RequestLoss] = []
        for workaround in list(attempt.applied):
            if workaround.apply_to_wire is None:
                continue
            fixed, losses = workaround.apply_to_wire(encoded)
            if fixed == encoded:
                attempt.applied.remove(workaround)
                attempt.idle.append(workaround)
                continue
            encoded = fixed
            wire_losses.extend(losses)
        attempt.prepared = Prepared(request, adapted, encoded, (*adapted.losses, *wire_losses))
        return attempt

    current = prepare(active)
    prepared, applied = current.ready, current.applied
    if policy.refuses(prepared.losses) is not None:
        return local_refusal(prepared.losses)
    result = await _send_prepared(client, codec, prepared, options_type, relay, relative_path)
    while isinstance(result, ModelCallFailure):
        failure = result
        candidates = [
            w
            for w in ledger.declared
            if w not in applied and w.name not in tried and w.recognizes(prepared, failure)
        ]
        retry: _Attempt | None = None
        workaround: RejectionWorkaround | None = None
        for workaround in candidates:
            tried.add(workaround.name)
            retry = prepare([*active, workaround])
            if workaround in retry.idle:
                # It would change nothing that is sent: no reason to retry, nor evidence.
                retry = None
                continue
            ledger.mark_provisional(workaround, request_fingerprint)
            if workaround not in retry.applied:
                return _refused_fix(failure, retry.kept_out)
            break
        if retry is None or workaround is None:
            for stale in applied:
                if stale.recognizes(prepared, failure):
                    ledger.forget(stale)  # applied, and the rejection came anyway
            break
        active.append(workaround)
        prepared, applied = retry.ready, retry.applied
        if delivered:
            await callback(StreamRestart())
        result = await _send_prepared(client, codec, prepared, options_type, relay, relative_path)
        if isinstance(result, ModelCallFailure) and workaround.recognizes(prepared, result):
            # The same rejection: it was not the fix. It leaves the attempt too, so a later
            # workaround's success cannot confirm it.
            ledger.forget(workaround)
            active.remove(workaround)
    if isinstance(result, ModelResponse):
        for confirmed in applied:
            ledger.confirm(confirmed)
    return result


@dataclasses.dataclass
class _Attempt:
    """One prepared attempt, and what became of the workarounds it was prepared with."""

    prepared: Prepared | None = None
    applied: list[RejectionWorkaround] = dataclasses.field(
        default_factory=list[RejectionWorkaround]
    )
    idle: list[RejectionWorkaround] = dataclasses.field(default_factory=list[RejectionWorkaround])
    kept_out: tuple[RequestLoss, ...] = ()

    @property
    def ready(self) -> Prepared:
        assert self.prepared is not None
        return self.prepared


def local_refusal(needed: tuple[RequestLoss, ...]) -> ModelCallFailure:
    """The failure of a call whose `AdaptationPolicy` refused a change in meaning before anything
    was sent: `adaptation_refused`, with no provider status, proposing the changes the request
    `needed` (the semantic ones are what the policy refused)."""
    return ModelCallFailure(
        category="adaptation_refused",
        description=(
            "not sent: the request cannot be sent without a change in meaning, which the call's "
            f"adaptation policy refuses ({_describe_semantic(needed)}); under "
            "semantic_changes='allow' it would be sent with that change"
        ),
        provider_http_status=None,
        proposed_adaptation_losses=needed,
    )


def _refused_fix(failure: ModelCallFailure, fix: tuple[RequestLoss, ...]) -> ModelCallFailure:
    """The provider's rejection, when the fix for it changes meaning and the policy refuses that:
    `adaptation_refused`, keeping the provider's status and message and the changes of what was
    sent, and proposing the refused `fix`."""
    if not fix:
        return failure
    return dataclasses.replace(
        failure,
        category="adaptation_refused",
        description=(
            f"{failure.description} (the request could be retried with a change in meaning, "
            f"which the call's adaptation policy refuses: {_describe_semantic(fix)}; under "
            "semantic_changes='allow' it would be)"
        ),
        proposed_adaptation_losses=fix,
    )


def _describe_semantic(losses: tuple[RequestLoss, ...]) -> str:
    return "; ".join(f"{loss.code}: {loss.detail}" for loss in losses if loss.semantic)


async def _send_prepared(
    client: SdkClient,
    codec: ModelSideCodec,
    prepared: Prepared,
    options_type: RequestOptionsType,
    callback: StreamEventCallback,
    relative_path: Callable[[str], str],
) -> ModelResponse | ModelCallFailure:
    """Send a prepared attempt and decode the provider's answer, carrying the attempt's losses."""
    encoded = prepared.encoded
    try:
        response = await post_raw(
            client, encoded, path=relative_path(encoded.path), options_type=options_type
        )
    except httpx2.HTTPError as exc:
        return dataclasses.replace(transport_failure(exc), adaptation_losses=prepared.losses)
    if response.status_code != 200:
        upstream = _UpstreamBody(response)
        try:
            body = b"".join([chunk async for chunk in upstream.chunks()])
        except httpx2.HTTPError as exc:
            return dataclasses.replace(transport_failure(exc), adaptation_losses=prepared.losses)
        finally:
            await upstream.aclose()
        status = response.status_code
        failure = _codec_step("decode_failure", lambda: codec.decode_failure(status, body))
        return dataclasses.replace(failure, adaptation_losses=prepared.losses)
    decoder = _codec_step("sse_decoder", codec.sse_decoder)
    result = await decode_stream(response, decoder, callback)
    return dataclasses.replace(result, adaptation_losses=prepared.losses)


async def forward_raw(
    client: SdkClient,
    target: str,
    headers: Headers,
    body: bytes,
    *,
    model: str,
    options_type: RequestOptionsType,
    relative_path: Callable[[str], str] = lambda path: path,
    place_model: Callable[[str, JSONObject, str], str] | None = None,
) -> RawHttpResponse:
    """Forward a harness request through an SDK client and return the raw response.

    `target` must be in origin form (`split_origin_form_target` refuses anything else, so the
    destination is always the client's configured base URL); `relative_path` maps its path to
    what that base URL expects (see `sdk_relative_path`). The harness's forwardable headers go
    over the SDK's defaults and `accept-encoding: identity` keeps the relayed body as it is; its
    query goes as sent, less credentials. The body is parsed and re-serialized with `model`
    rewritten: its `model` field, or, for a dialect that names the model in the path,
    wherever `place_model` puts it (given the path, the body and the model, it returns the
    path). Transport failures propagate as httpx2 errors for the caller to classify.
    """
    path, query = split_origin_form_target(target)
    query = forwardable_query(query)
    spelling = _spelling(client)
    request_headers = _joined(forwardable_headers(headers), spelling)
    request_headers[spelling.get("accept-encoding", "Accept-Encoding")] = "identity"
    payload = decode_json_object(body, "forwarded request body")
    if place_model is None:
        payload["model"] = model
    else:
        path = place_model(path, payload, model)
    response = await _send(
        client,
        options_type,
        path=relative_path(path),
        query=query,
        headers=request_headers,
        body=payload,
    )
    upstream = _UpstreamBody(response)
    return RawHttpResponse(
        status=response.status_code,
        headers=_relayed_headers(response.headers),
        body=upstream.chunks(),
        aclose=upstream.aclose,
    )


# Upstream response headers not relayed: whoever relays the body re-frames it, and the
# body it relays is already decoded.
_UNRELAYED_RESPONSE_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}
)


def _relayed_headers(headers: httpx2.Headers) -> Headers:
    return tuple(
        (name, value)
        for name, value in headers.multi_items()
        if name.lower() not in _UNRELAYED_RESPONSE_HEADERS
    )


async def decode_stream(
    response: httpx2.Response, decoder: SseDecoder, callback: StreamEventCallback
) -> ModelResponse | ModelCallFailure:
    """Feed the streamed body to `decoder`, relaying events; the connection is released on every
    exit. A transport failure mid-stream is a failure result (the provider stopped answering),
    not an exception. A chunk that decodes to no event (a provider ping, or part of a frame) is
    relayed as a `StreamKeepalive`, so a consumer can tell a live stream from a stalled one."""
    upstream = _UpstreamBody(response)
    try:
        async for chunk in upstream.chunks():
            piece = chunk
            events = _codec_step("decode_response", lambda: decoder.feed(piece))
            if not events:
                await callback(StreamKeepalive())
            for event in events:
                await callback(event)
    except httpx2.HTTPError as exc:
        if _raised_while_cancelled(exc):
            # httpx2 closes the response inside the iteration when it is cancelled, so a failing
            # close surfaces here in place of the cancellation; returning a result would swallow
            # it.
            raise
        return ModelCallFailure(
            category="overloaded",
            description=f"stream failed mid-response: {exc}"[:MAX_FAILURE_DESCRIPTION],
            provider_http_status=None,
        )
    finally:
        await upstream.aclose()
    return _codec_step("decode_response", decoder.finish)


def _raised_while_cancelled(exc: BaseException) -> bool:
    """Whether `exc` was raised while a cancellation was being handled."""
    cancelled = anyio.get_cancelled_exc_class()
    context = exc.__context__
    while context is not None:
        if isinstance(context, cancelled):
            return True
        context = context.__context__
    return False


def transport_failure(exc: Exception) -> ModelCallFailure:
    """A connection failure or timeout raised before any response arrived: the provider is
    unreachable or unresponsive, which consumers treat like an overload."""
    return ModelCallFailure(
        category="overloaded",
        description=str(exc)[:MAX_FAILURE_DESCRIPTION],
        provider_http_status=None,
    )


def sdk_relative_path(path: str) -> str:
    """The dialect path (`/v1/responses`) as the OpenAI SDK expects it: relative to a base URL
    that already ends in the API version (`https://api.openai.com/v1/`, OpenRouter's
    `/api/v1`, Gemini's `/v1beta/openai/`)."""
    return path[len("/v1") :] if path.startswith("/v1/") else path
