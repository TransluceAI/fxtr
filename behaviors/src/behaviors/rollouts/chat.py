"""Chat rollouts: a chat session model exchanging turns with a context policy.

The runner owns session lifetimes, reports completed turns and streaming
events, and records configured model failures on the returned rollout.
"""

import dataclasses
from collections.abc import Awaitable, Callable, Collection
from typing import Literal

from behaviors.interfaces._sessions import closing_session
from behaviors.interfaces.chat_session_models import (
    ChatSession,
    ChatSessionModel,
    StreamingChatSession,
)
from behaviors.interfaces.context_policies import ContextPolicy
from behaviors.interfaces.model_apis import ModelCallFailedError
from behaviors.types.agent_run import AgentRun, Transcript
from behaviors.types.failure import FailureCategory, ModelCallFailure
from behaviors.types.message import ChatMessage
from behaviors.types.rollout import (
    ChatRollout,
    ContextTurn,
    EndRollout,
    ModelTurn,
    RolloutCompleted,
    RolloutFailed,
    RolloutMaxTurns,
    RolloutStatus,
    SetSystemMessage,
)
from behaviors.types.streaming import StreamEvent
from fxtr.entity_defns.dynamic_data import DynamicData


@dataclasses.dataclass(frozen=True)
class ContextTurnEvent:
    """The context policy produced a turn — the same object that lands on the rollout.

    Attributes:
        turn: The context turn.
    """

    turn: ContextTurn
    kind: Literal["context_turn_event"] = "context_turn_event"


@dataclasses.dataclass(frozen=True)
class ModelTurnEvent:
    """The model completed a turn — the same object that lands on the rollout.

    Attributes:
        turn: The model turn.
    """

    turn: ModelTurn
    kind: Literal["model_turn_event"] = "model_turn_event"


@dataclasses.dataclass(frozen=True)
class ModelStreamEvent:
    """A delta from the model while its turn is being generated.

    Emitted only when the model session is a ``StreamingChatSession``. Deltas
    may arrive for a turn that ultimately fails (including retries — see
    ``StreamRestart``); the assembled turn follows as a ``ModelTurnEvent``
    only on success.

    Attributes:
        event: The stream event, as the session surfaced it.
    """

    event: StreamEvent
    kind: Literal["model_stream_event"] = "model_stream_event"


ChatRolloutEvent = ContextTurnEvent | ModelTurnEvent | ModelStreamEvent

type ChatRolloutEventCallback = Callable[[ChatRolloutEvent], Awaitable[None]]
"""Receives each rollout event as it happens, for watching a rollout live.

The callback is awaited on the rollout's own path: events arrive in order, a
slow callback slows the rollout, and an exception it raises aborts the
rollout. That also means the callback can deliberately pace or gate the
rollout by not returning until it wants the next step to run.
"""


async def run_chat_rollout(
    model: ChatSessionModel,
    context_policy: ContextPolicy,
    *,
    max_turns: int | None = None,
    failure_categories_to_record: Collection[FailureCategory] = ("context_length",),
    event_callback: ChatRolloutEventCallback | None = None,
) -> ChatRollout:
    """Run one rollout of a chat session model against a context policy.

    The context policy generates the model's session initialization, then the
    two exchange turns until the policy ends the rollout (its EndRollout is
    recorded on ``ChatRollout.end``), ``max_turns`` turns have elapsed, or a
    model call fails. A failure in one of ``failure_categories_to_record``
    still returns the rollout — status ``RolloutFailed``, transcript so far
    preserved — while any other failure raises ``ModelCallFailedError``,
    producing no rollout. Other exceptions from either side also propagate.

    ``event_callback``, when given, receives each turn as it is produced —
    the same objects that land on the returned rollout — and, when the
    model's session is a ``StreamingChatSession``, the model's delta events
    while each turn is generated. How the rollout ends is not an event; it is
    on the returned rollout. See ``ChatRolloutEventCallback`` for the
    delivery contract.
    """
    if max_turns is not None and max_turns < 0:
        raise ValueError("max_turns must be nonnegative")
    context_turns: list[ContextTurn] = []
    model_turns: list[ModelTurn] = []
    status: RolloutStatus
    end: EndRollout | None

    async def generate_model_turn(
        model_session: ChatSession, context_turn: ContextTurn
    ) -> ModelTurn | ModelCallFailure:
        if event_callback is not None and isinstance(model_session, StreamingChatSession):
            callback = event_callback

            async def forward(delta: StreamEvent) -> None:
                await callback(ModelStreamEvent(event=delta))

            return await model_session.generate_turn_with_events(context_turn, forward)
        return await model_session.generate_turn(context_turn)

    async with closing_session(await context_policy.start_session()) as policy_session:
        initialization = await policy_session.generate_initialization()
        async with closing_session(await model.start_session(initialization)) as model_session:
            last_model_turn: ModelTurn | None = None
            while True:
                if max_turns is not None and len(model_turns) >= max_turns:
                    status = RolloutMaxTurns(max_turns=max_turns)
                    end = None
                    break
                step = await policy_session.generate_context_turn(last_model_turn)
                if isinstance(step, EndRollout):
                    status = RolloutCompleted()
                    end = step
                    break
                context_turns.append(step)
                if event_callback is not None:
                    await event_callback(ContextTurnEvent(turn=step))
                model_turn = await generate_model_turn(model_session, step)
                if isinstance(model_turn, ModelCallFailure):
                    if model_turn.category not in failure_categories_to_record:
                        raise ModelCallFailedError(model_turn)
                    status = RolloutFailed(failure=model_turn)
                    end = None
                    break
                model_turns.append(model_turn)
                if event_callback is not None:
                    await event_callback(ModelTurnEvent(turn=model_turn))
                last_model_turn = model_turn

    return ChatRollout(
        context_turns=tuple(context_turns),
        model_turns=tuple(model_turns),
        status=status,
        end=end,
        initialization=(
            DynamicData.from_tagged(initialization) if initialization is not None else None
        ),
    )


class ChatRolloutTranscriptProjection:
    """Flattens a rollout's turns into a single transcript.

    A recorded SetSystemMessage precedes the first context turn; other
    initialization types are not represented. The transcript ends with the
    unanswered context turn when the rollout ended on a recorded failure.
    Per-turn metadata is not represented; rollout kinds whose judges need it
    should use their own projection.
    """

    async def to_transcript(self, result: ChatRollout) -> Transcript:
        messages: list[ChatMessage] = []
        # TODO: Make this generic over session initialization types instead of
        # special-casing SetSystemMessage, e.g. by letting initializations render
        # their own messages and having the projection take the types it handles.
        set_system = (
            result.initialization.try_as_type(SetSystemMessage)
            if result.initialization is not None
            else None
        )
        if set_system is not None:
            messages.append(set_system.message)
        for i, context_turn in enumerate(result.context_turns):
            messages.extend(context_turn.messages)
            if i < len(result.model_turns):
                messages.extend(result.model_turns[i].messages)
        return Transcript(
            name=None,
            description=None,
            messages=tuple(messages),
        )

    async def to_agent_run(self, result: ChatRollout) -> AgentRun:
        return AgentRun(
            name=None,
            description=None,
            transcripts=(await self.to_transcript(result),),
            groups=(),
        )
