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
"""

from typing import Protocol, runtime_checkable

from behaviors.types.failure import ModelCallFailure
from behaviors.types.response import ModelRequest, ModelResponse
from behaviors.types.streaming import StreamEventCallback


class ModelError(Exception):
    """Base class for errors raised by model implementations."""


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
        ``StopContentFilter`` stop reason.
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
        ``StreamRestart`` if it restarts generation (see
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
