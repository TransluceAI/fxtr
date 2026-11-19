"""Model API protocols: chat-completions-shaped LLM APIs with the model built in.

A ``ModelAPI`` holds no conversation state; every call carries the full
context. This is the common interface for model wrappers and rollout
consumers, and the raw material for chat session models (see
``behaviors.interfaces.chat_session_models``).

A call that cannot produce a response reports a ``ModelCallFailure`` as data
(see ``behaviors.types.failure``) rather than raising, and implementations do
not retry internally — retry policy belongs to consumers (see
``behaviors.types.retry``), because which failures are worth retrying is
consumer-relative. Exceptions are reserved for the implementation itself
being broken or misused (``ModelError``), and for consumers converting a
failure they cannot handle (``ModelCallFailedError``); both apply equally to
chat session models.

``DescribedModelAPI`` and ``CodecModelAPI`` are optional capabilities an implementation can add:
saying what model and wire dialect it serves, and serving a request written in that dialect
verbatim (for a server that passes a harness's requests through in their own dialect).
"""

from typing import Protocol, runtime_checkable

from behaviors.interfaces.dialect_codecs import ModelSideCodec
from behaviors.types.dialect import Dialect, Headers, RawHttpResponse
from behaviors.types.failure import ModelCallFailure
from behaviors.types.response import ModelRequest, ModelResponse
from behaviors.types.streaming import StreamEventCallback


class ModelError(Exception):
    """Base class for errors raised by model implementations."""


class ForwardTransportError(ModelError):
    """A forwarded request (`CodecModelAPI.forward_request`) never got a response: the provider
    was unreachable, the connection failed, or the request timed out before any status arrived.
    A non-success status is not an error but a returned response."""


class ModelCallFailedError(ModelError):
    """A model call failed and the caller chose not to handle the failure as data.

    Raised by consumers of ``ModelCallFailure``, e.g. a rollout runner hitting
    a failure category it was not configured to record.

    Attributes:
        failure: The underlying failure.
    """

    def __init__(self, failure: ModelCallFailure):
        super().__init__(f"{failure.category}: {failure.description}")
        self.failure = failure


@runtime_checkable
class ModelAPI(Protocol):
    """An LLM API with the model built in, invoked with full context on every call."""

    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        """Generate one response for the given request.

        Failures are returned as data, exactly once per call — no internal
        retries. A provider content filter that stops generation after it
        starts is not a failure: it yields a normal response with a
        ``StopRefusal`` (or ``StopContentFilter``) stop reason.
        """
        ...


@runtime_checkable
class StreamingModelAPI(ModelAPI, Protocol):
    """A model API that can additionally stream its responses."""

    async def generate_with_events(
        self, request: ModelRequest, callback: StreamEventCallback
    ) -> ModelResponse | ModelCallFailure:
        """Generate one response, passing events to `callback` as they arrive.

        Returns the fully assembled response — identical to what `generate`
        would have returned — so callbacks never assemble content. An
        implementation whose provider stream dies mid-response emits
        ``StreamRestart`` if it restarts generation, and may emit
        ``StreamKeepalive`` for stream bytes that carry no content (a provider's
        pings), which a consumer that wants only content ignores (see
        ``behaviors.types.streaming``).
        """
        ...


@runtime_checkable
class ModelProvider(Protocol):
    """A source of ``ModelAPI``s looked up by model id.

    Where a ``ModelAPI`` serves one fixed model, a provider serves whichever
    model it is asked for — e.g. an agent harness whose main-thread and
    utility traffic request different model ids. Repeated lookups of the same
    id return the same instance where that matters (implementations cache).
    """

    def get_model_api(self, model_id: str) -> ModelAPI | None:
        """The ``ModelAPI`` serving ``model_id``, or None if this provider cannot serve it."""
        ...


@runtime_checkable
class DescribedModelAPI(Protocol):
    """What a model API is, for diagnostics: its resolved model id and the dialect it speaks.

    Implementations expose both. A wrapper that delegates calls to another API does not
    describe itself; it exposes the API it wraps (``WrappingModelAPI``) and ``describe`` looks
    through it.
    """

    @property
    def model_id(self) -> str: ...

    @property
    def dialect(self) -> Dialect | None:
        """The wire dialect the API speaks, or None for an API without a fixed one."""
        ...


@runtime_checkable
class CodecModelAPI(DescribedModelAPI, StreamingModelAPI, Protocol):
    """A model API built on a dialect codec, able to serve a request in that dialect verbatim."""

    @property
    def codec(self) -> ModelSideCodec: ...

    async def forward_request(self, target: str, headers: Headers, body: bytes) -> RawHttpResponse:
        """POST ``body`` to ``target`` (a path plus optional query) upstream, as the harness
        sent it.

        The destination is this API's configured client; ``target`` must be in origin form and
        a ``ValueError`` refuses anything else (an absolute URL would redirect this API's
        credentials). Hop-by-hop, credential and accept-encoding headers are dropped and this
        API's own credentials added, ``accept-encoding: identity`` keeps the relayed body
        byte-transparent, and ``body["model"]`` is rewritten to this API's model (the body is
        re-serialized, so it is JSON-equivalent to the harness's rather than byte-identical).
        Every status is returned as-is, streamed; a transport failure raises
        ``ForwardTransportError``. Cancelling the awaiting task cancels the upstream request.
        """
        ...


@runtime_checkable
class WrappingModelAPI(Protocol):
    """A model API that delegates to another one (sampling overrides, a request clamp, ...)."""

    @property
    def inner(self) -> ModelAPI: ...


def describe(api: ModelAPI) -> DescribedModelAPI | None:
    """The description of ``api``, looking through wrappers; None when nothing describes it.

    Wrappers cannot opt in to `DescribedModelAPI` conditionally, which is why this walks `inner`
    instead: on Python 3.12 a runtime-checkable `isinstance` looks members up with
    `inspect.getattr_static`, so a `model_id` property that raises `AttributeError` when the
    wrapped model has none still counts as present, and the wrapper would describe itself with an
    exception.
    """
    seen: set[int] = set()
    while id(api) not in seen:
        seen.add(id(api))
        if isinstance(api, DescribedModelAPI):
            return api
        if not isinstance(api, WrappingModelAPI):
            return None
        api = api.inner
    return None
