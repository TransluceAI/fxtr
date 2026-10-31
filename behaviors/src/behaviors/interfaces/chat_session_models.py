"""Chat session model protocols: models that run single, linear chat sessions.

A ``ChatSessionModel`` is anything whose conversation lives somewhere other
than the caller: a model API wrapped with its accumulated messages
(``behaviors.implementations.models.from_api``), a browser-automated chatbot
UI, an agent harness in a container. A session receives a context turn and
produces a model turn; either side may consist of multiple messages, and
implementations attach extra per-turn information to ``ModelTurn.metadata``
(see ``behaviors.types.rollout``). Session models that only accept restricted
context turns (e.g. a browser-automated UI that cannot take tools or multiple
messages) enforce that at runtime, as they do for the initialization types
they support.

The serializable variants additionally expose the session state as a tagged
value (held as ``DynamicData``) that can be captured and later restored into
a fresh session — including on a different process — which is what enables
checkpointing and tree/vine sampling. A serializable session is also a plain
session, so the two kinds are interchangeable wherever only the plain
protocol is required.

The streaming variants push delta events to a callback while still returning
the fully assembled turn, so streaming consumers (progress UIs, proxies)
never assemble content and non-streaming consumers can ignore the variants
entirely.

Failures follow ``behaviors.interfaces.model_apis``: a turn that cannot be
produced is a ``ModelCallFailure`` value, and exceptions (``ModelError``) are
reserved for broken or misused implementations.
"""

from typing import Protocol, runtime_checkable

from behaviors.types.failure import ModelCallFailure
from behaviors.types.rollout import ContextTurn, ModelTurn, SessionInitialization
from behaviors.types.streaming import StreamEventCallback
from fxtr.entity_defns.dynamic_data import DynamicData


@runtime_checkable
class ChatSession(Protocol):
    """One conversation with a chat session model."""

    async def generate_turn(self, context_turn: ContextTurn) -> ModelTurn | ModelCallFailure:
        """Return the model's turn in response to the given context turn.

        A failure leaves the session state as it was before the call, so the
        caller may try a different context turn.
        """
        ...

    async def aclose(self) -> None:
        """Release the session's resources; the session is unusable afterwards.

        A no-op if there is nothing to release. Implementations may assume it
        is called exactly once, even after a natural end of the conversation.
        The name makes sessions compatible with ``contextlib.aclosing``.
        """
        ...


@runtime_checkable
class ChatSessionModel(Protocol):
    """A model that runs single, linear chat sessions."""

    async def start_session(self, initialization: SessionInitialization | None) -> ChatSession:
        """Return a fresh session, independent of any other session.

        The initialization carries whatever the model needs before the first
        turn (e.g. ``SetSystemMessage`` for a chat model, or a scenario
        setup), or None for models that need nothing; in a rollout it is
        produced by the context policy. Implementations must check the
        initialization's type and raise ``ModelError`` for kinds they do not
        support.
        """
        ...


@runtime_checkable
class StreamingChatSession(ChatSession, Protocol):
    """A model session that can additionally stream its turns."""

    async def generate_turn_with_events(
        self, context_turn: ContextTurn, callback: StreamEventCallback
    ) -> ModelTurn | ModelCallFailure:
        """Generate one turn, passing events to `callback` as they arrive.

        Returns the same turn `generate_turn` would have; the event contract
        matches ``StreamingModelAPI.generate_with_events``.
        """
        ...


@runtime_checkable
class StreamingChatSessionModel(ChatSessionModel, Protocol):
    """A chat session model whose sessions can stream their turns."""

    async def start_session(
        self, initialization: SessionInitialization | None
    ) -> StreamingChatSession: ...


@runtime_checkable
class SerializableChatSession(ChatSession, Protocol):
    """A model session whose state can be captured."""

    async def get_state(self) -> DynamicData:
        """Return the session's current state.

        Only returns the state — persisting it is up to the caller (fxtr
        storage, or ``thaw_fxtr_data(state.data)`` for plain JSON, rebuilt with
        ``DynamicData.from_data``). The returned value is immutable with
        respect to further turns in this session, and restoring it (see
        ``SerializableChatSessionModel.restore_session``) yields a session that
        behaves as this one would have at the time of the call. The state's
        type is implementation-defined; implementations use a tagged dataclass
        of their own, so state from a different implementation is rejected by
        its tag.
        """
        ...


@runtime_checkable
class SerializableChatSessionModel(ChatSessionModel, Protocol):
    """A chat session model whose sessions have serializable state."""

    async def start_session(
        self, initialization: SessionInitialization | None
    ) -> SerializableChatSession: ...

    async def restore_session(self, state: DynamicData) -> SerializableChatSession:
        """Start a session that continues from previously captured state.

        Implementations decode the state as their own state type, which raises
        (``ConversionError``) on state this model's sessions did not produce.
        """
        ...


@runtime_checkable
class SerializableStreamingChatSession(SerializableChatSession, StreamingChatSession, Protocol):
    """A chat session that is both serializable and streaming."""


@runtime_checkable
class SerializableStreamingChatSessionModel(
    SerializableChatSessionModel,
    StreamingChatSessionModel,
    Protocol,
):
    """A chat session model whose sessions are both serializable and streaming."""

    async def start_session(
        self, initialization: SessionInitialization | None
    ) -> SerializableStreamingChatSession: ...

    async def restore_session(self, state: DynamicData) -> SerializableStreamingChatSession: ...
