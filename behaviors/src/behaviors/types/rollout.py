"""ChatRollout types: alternating context/model turns and their outcome.

Implementations record extra information on the ``metadata`` field that
``ContextTurn``, ``ModelTurn``, ``UserTurn``, ``ToolTurn``, ``EndRollout``,
and ``ChatRollout`` all carry: a tuple of tagged values whose types the
producer defines (a browser model's info-box text, a user simulator's pilot
logs, a measured final stance). Each entry is held as ``DynamicData`` — its
serialized form, type tag included — so the rollout types never need to know
the producers' types. A consumer reads the entries it understands by naming
their type (``find_metadata``, or ``DynamicData.try_as_type``).
"""

import dataclasses
from typing import Literal

from behaviors.types.failure import ModelCallFailure
from behaviors.types.message import ChatMessage, SystemMessage, UserMessage
from behaviors.types.namespace import behaviors_type
from behaviors.types.tool import ToolDefinition
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.dynamic_data import DynamicData


def find_metadata[T: TaggedDataclass](metadata: tuple[DynamicData, ...], cls: type[T]) -> T | None:
    """Return the first metadata entry of type `cls`, decoded, or None if there is none.

    Raises:
        ConversionError: If an entry is tagged as `cls` but does not decode as it.
    """
    for entry in metadata:
        if (value := entry.try_as_type(cls)) is not None:
            return value
    return None


@dataclasses.dataclass(frozen=True)
class ContextTurn(
    TaggedDataclass,
    fxtr_type=behaviors_type("ContextTurn.v2"),
):
    """A context-policy turn: what the model is shown next.

    Forms a tagged union with ``EndRollout`` in
    ``ContextPolicySession.generate_context_turn``'s return type; the tag is
    what lets the union survive serialization.

    Attributes:
        messages: The messages added to the conversation this turn.
        available_tools: The tools available to the model for the next turn, or
            None if tools were not offered.
        metadata: Extra information the producer chose to record.
    """

    messages: tuple[ChatMessage, ...]
    available_tools: tuple[ToolDefinition, ...] | None
    kind: Literal["context_turn"] = "context_turn"
    metadata: tuple[DynamicData, ...] = ()


@dataclasses.dataclass(frozen=True)
class ModelTurn(
    TaggedDataclass,
    fxtr_type=behaviors_type("ModelTurn.v2"),
):
    """A model turn.

    Generally a single assistant message, but some models (e.g. ones with
    backend-implemented tools such as web search) may produce multiple
    messages per turn. The tag distinguishes a serialized turn from a
    serialized ``ModelCallFailure``.

    Attributes:
        messages: The messages the model produced this turn.
        metadata: Extra information the model implementation chose to record
            (e.g. the raw API response, or what a browser UI displayed
            alongside the reply).
    """

    messages: tuple[ChatMessage, ...]
    kind: Literal["model_turn"] = "model_turn"
    metadata: tuple[DynamicData, ...] = ()


@dataclasses.dataclass(frozen=True)
class SessionInitialization(DataclassEntity):
    """Base class for chat session initializations.

    An initialization carries whatever a session model needs before the first
    turn. Subclasses must pass their own reverse-domain ``fxtr_type`` string;
    use a domain you control to keep external initialization types distinct.
    Models reject
    initialization types they do not support (see
    ``behaviors.interfaces.chat_session_models.ChatSessionModel.start_session``).
    """


@dataclasses.dataclass(frozen=True)
class SetSystemMessage(SessionInitialization, fxtr_type=behaviors_type("SetSystemMessage.v1")):
    """Session initialization that sets the conversation's system message.

    Attributes:
        message: The system message the conversation starts with.
    """

    message: SystemMessage


@dataclasses.dataclass(frozen=True, kw_only=True)
class EndRollout(
    TaggedDataclass,
    fxtr_type=behaviors_type("EndRollout.v2"),
):
    """Signals that the rollout should end.

    Returned by context policies (and user simulators) instead of another
    turn; the instance is recorded on ``ChatRollout.end``. End-of-rollout
    information — an agentic environment's success/failure, a user
    simulator's predicted stance — goes in ``metadata``.
    """

    kind: Literal["end_rollout"] = "end_rollout"
    metadata: tuple[DynamicData, ...] = ()


@dataclasses.dataclass(frozen=True, kw_only=True)
class UserTurn(
    TaggedDataclass,
    fxtr_type=behaviors_type("UserTurn.v2"),
):
    """A user simulator turn.

    Forms a tagged union with ``EndRollout`` in
    ``UserSimulatorSession.next_user_turn``'s return type. Extra per-turn
    information (pilot provenance, debug info) goes in ``metadata``.

    Attributes:
        message: The user message to show the model.
        metadata: Extra information the simulator chose to record.
    """

    message: UserMessage
    kind: Literal["user_turn"] = "user_turn"
    metadata: tuple[DynamicData, ...] = ()


@dataclasses.dataclass(frozen=True, kw_only=True)
class ToolTurn(
    TaggedDataclass,
    fxtr_type=behaviors_type("ToolTurn.v2"),
):
    """A tool policy turn: the results of the requested tool calls.

    Returned by ``ToolPolicySession.generate_tool_turn``. Extra per-turn
    information (exit codes, timings, environment observations) goes in
    ``metadata``.

    Attributes:
        messages: One tool result per requested tool use, in order.
        metadata: Extra information the tool policy chose to record.
    """

    messages: tuple[ChatMessage, ...]
    kind: Literal["tool_turn"] = "tool_turn"
    metadata: tuple[DynamicData, ...] = ()


@dataclasses.dataclass(frozen=True)
class RolloutCompleted(TaggedDataclass, fxtr_type=behaviors_type("RolloutCompleted.v1")):
    """The context policy ended the rollout; ``ChatRollout.end`` holds its EndRollout."""

    kind: Literal["rollout_completed"] = "rollout_completed"


@dataclasses.dataclass(frozen=True)
class RolloutMaxTurns(TaggedDataclass, fxtr_type=behaviors_type("RolloutMaxTurns.v1")):
    """The rollout was cut off at the turn limit before the context policy ended it.

    Attributes:
        max_turns: The limit that was reached.
    """

    max_turns: int
    kind: Literal["rollout_max_turns"] = "rollout_max_turns"


@dataclasses.dataclass(frozen=True)
class RolloutFailed(TaggedDataclass, fxtr_type=behaviors_type("RolloutFailed.v1")):
    """A model call failure ended the rollout, with the transcript so far preserved.

    Only failure categories the rollout runner was configured to record end a
    rollout this way (see ``behaviors.rollouts.chat.run_chat_rollout``);
    other categories raise instead, producing no rollout at all. The context
    turn that triggered the failure is the last element of
    ``ChatRollout.context_turns``.

    Attributes:
        failure: The failure the model returned.
    """

    failure: ModelCallFailure
    kind: Literal["rollout_failed"] = "rollout_failed"


RolloutStatus = RolloutCompleted | RolloutMaxTurns | RolloutFailed


@dataclasses.dataclass(frozen=True)
class ChatRollout(
    DataclassEntity,
    fxtr_type=behaviors_type("ChatRollout.v2"),
):
    """A rollout: alternating context and model turns, and an outcome.

    Turns alternate starting with a context turn: ``context_turns[i]`` was
    answered by ``model_turns[i]``. The tuples are equal length, except that
    ``context_turns`` has one extra, unanswered element when the rollout
    ended on a recorded model call failure (see RolloutFailed).

    A rollout that fails outright (infrastructure error, bug) is not a
    ChatRollout: ``behaviors.rollouts.chat.run_chat_rollout`` raises instead,
    and recording the failure is its caller's concern.

    Attributes:
        context_turns: The context policy's turns, in order.
        model_turns: The model's turns, in order.
        status: Whether the rollout completed, hit the turn limit, or ended on
            a recorded model call failure.
        end: The context policy's EndRollout, carrying any end-of-rollout
            information in its metadata; present exactly when the rollout
            completed.
        metadata: Extra run-level information the caller chose to record.
        initialization: The initialization passed to the model before the first
            turn, or None if there was none. Held as ``DynamicData`` because
            initialization types are open-ended; read it with
            ``initialization.try_as_type(SetSystemMessage)`` (or another type
            the reader handles).
    """

    context_turns: tuple[ContextTurn, ...]
    model_turns: tuple[ModelTurn, ...]
    status: RolloutStatus
    end: EndRollout | None
    metadata: tuple[DynamicData, ...] = ()
    initialization: DynamicData | None = None
