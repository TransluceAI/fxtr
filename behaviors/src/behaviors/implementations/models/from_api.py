"""Chat session models backed by a model API.

``chat_session_model_from_api`` turns any ``ModelAPI`` into a chat session
model by accumulating the conversation's messages as session state. This is
the standard way to run an API model in a rollout. Each turn's metadata
carries the underlying ``ModelResponse`` — how and why generation ended and
what it cost — readable with ``find_metadata(turn.metadata, ModelResponse)``.
"""

import dataclasses
from collections.abc import Collection
from typing import overload

from behaviors.implementations.models.retries import (
    DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
    generate_with_retries,
)
from behaviors.interfaces.chat_session_models import (
    SerializableChatSessionModel,
    SerializableStreamingChatSessionModel,
)
from behaviors.interfaces.model_apis import ModelAPI, ModelError, StreamingModelAPI
from behaviors.types.failure import FailureCategory, ModelCallFailure
from behaviors.types.message import ChatMessage, SystemMessage
from behaviors.types.namespace import behaviors_type
from behaviors.types.response import ModelRequest, ModelResponse, SamplingParams
from behaviors.types.retry import RetryConfig
from behaviors.types.rollout import ContextTurn, ModelTurn, SessionInitialization, SetSystemMessage
from behaviors.types.streaming import StreamEventCallback
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData


@dataclasses.dataclass(frozen=True)
class _SessionState(TaggedDataclass, fxtr_type=behaviors_type("APIBackedChatSessionState.v1")):
    """The captured state of an ``APIBackedChatSession``.

    Attributes:
        messages: The conversation so far, including the model's initial messages and the
            initialization's system message.
    """

    messages: tuple[ChatMessage, ...]


class APIBackedChatSession:
    """A conversation with a model API, held as accumulated messages.

    Turns can always be streamed; if the underlying API is not a
    ``StreamingModelAPI``, a streamed turn just emits no delta events.
    """

    def __init__(
        self,
        model: ModelAPI,
        sampling: SamplingParams,
        retry: RetryConfig | None,
        failure_categories_to_retry: Collection[FailureCategory],
        messages: tuple[ChatMessage, ...],
    ):
        self._model = model
        self._sampling = sampling
        self._retry = retry
        self._failure_categories_to_retry = failure_categories_to_retry
        self._messages = messages

    async def _turn(
        self, context_turn: ContextTurn, callback: StreamEventCallback | None
    ) -> ModelTurn | ModelCallFailure:
        context = self._messages + context_turn.messages
        request = ModelRequest(
            context=context,
            tools=context_turn.available_tools or (),
            tool_choice=None,
            parallel_tool_calls=None,
            sampling=self._sampling,
        )
        response = await generate_with_retries(
            self._model,
            request,
            retry=self._retry,
            failure_categories_to_retry=self._failure_categories_to_retry,
            callback=callback,
        )
        if not isinstance(response, ModelResponse):
            return response
        self._messages = context + response.output
        return ModelTurn(
            messages=response.output,
            metadata=(DynamicData.from_tagged(response),),
        )

    async def generate_turn(self, context_turn: ContextTurn) -> ModelTurn | ModelCallFailure:
        return await self._turn(context_turn, None)

    async def generate_turn_with_events(
        self, context_turn: ContextTurn, callback: StreamEventCallback
    ) -> ModelTurn | ModelCallFailure:
        return await self._turn(context_turn, callback)

    async def get_state(self) -> DynamicData:
        return DynamicData.from_tagged(_SessionState(messages=self._messages))

    async def aclose(self) -> None:
        pass


class _APIBackedChatSessionModel:
    def __init__(
        self,
        model: ModelAPI,
        sampling: SamplingParams,
        retry: RetryConfig | None,
        failure_categories_to_retry: Collection[FailureCategory],
        initial_messages: tuple[ChatMessage, ...],
    ):
        self._model = model
        self._sampling = sampling
        self._retry = retry
        self._failure_categories_to_retry = failure_categories_to_retry
        self._initial_messages = initial_messages

    def _session(self, messages: tuple[ChatMessage, ...]) -> APIBackedChatSession:
        return APIBackedChatSession(
            self._model, self._sampling, self._retry, self._failure_categories_to_retry, messages
        )

    async def start_session(
        self, initialization: SessionInitialization | None
    ) -> APIBackedChatSession:
        if initialization is None:
            return self._session(self._initial_messages)
        if not isinstance(initialization, SetSystemMessage):
            raise ModelError(
                f"API-backed sessions do not support {type(initialization).__name__} initialization"
            )
        if any(isinstance(message, SystemMessage) for message in self._initial_messages):
            raise ModelError(
                "the context policy set a system message, but this model's initial messages"
                " already include one"
            )
        return self._session(self._initial_messages + (initialization.message,))

    async def restore_session(self, state: DynamicData) -> APIBackedChatSession:
        return self._session(state.as_type(_SessionState).messages)


@overload
def chat_session_model_from_api(
    model_api: StreamingModelAPI,
    sampling: SamplingParams,
    retry: RetryConfig | None = None,
    failure_categories_to_retry: Collection[FailureCategory] = DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
    *,
    initial_messages: tuple[ChatMessage, ...] = (),
) -> SerializableStreamingChatSessionModel: ...


@overload
def chat_session_model_from_api(
    model_api: ModelAPI,
    sampling: SamplingParams,
    retry: RetryConfig | None = None,
    failure_categories_to_retry: Collection[FailureCategory] = DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
    *,
    initial_messages: tuple[ChatMessage, ...] = (),
) -> SerializableChatSessionModel: ...


def chat_session_model_from_api(
    model_api: ModelAPI,
    sampling: SamplingParams,
    retry: RetryConfig | None = None,
    failure_categories_to_retry: Collection[FailureCategory] = DEFAULT_FAILURE_CATEGORIES_TO_RETRY,
    *,
    initial_messages: tuple[ChatMessage, ...] = (),
) -> SerializableChatSessionModel:
    """Make a chat session model whose state is the conversation with `model_api`.

    Sessions start from `initial_messages` (e.g. a system prompt that is part
    of the model's configuration), followed by the context policy's optional
    ``SetSystemMessage``; other initialization types raise ``ModelError``, as
    does a ``SetSystemMessage`` when `initial_messages` already holds a
    ``SystemMessage``. Every request uses `sampling`. Given a ``StreamingModelAPI``, the result is a
    ``SerializableStreamingChatSessionModel`` whose sessions stream deltas;
    otherwise streaming a turn just emits no delta events. With a `retry`
    schedule, failures in `failure_categories_to_retry` are retried with
    backoff (a streamed turn emits ``StreamRestart`` before each retry); once
    attempts are exhausted the last failure is returned as data from the
    turn, like any other failure.
    """
    return _APIBackedChatSessionModel(
        model_api, sampling, retry, failure_categories_to_retry, initial_messages
    )
