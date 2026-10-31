"""A user simulator that sends a fixed list of messages."""

import dataclasses

from behaviors.interfaces.context_policies import SerializableUserSimulatorSession
from behaviors.types.message import ChatMessage, UserMessage
from behaviors.types.namespace import behaviors_type
from behaviors.types.rollout import EndRollout, UserTurn
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData


@dataclasses.dataclass(frozen=True)
class _ScriptedState(TaggedDataclass, fxtr_type=behaviors_type("ScriptedUserSimulatorState.v1")):
    """The user messages a scripted session has not sent yet."""

    messages: tuple[UserMessage, ...]


class _ScriptedUserSimulatorSession:
    def __init__(self, messages: tuple[UserMessage, ...]):
        self._messages = list(messages)

    async def next_user_turn(
        self,
        model_messages: tuple[ChatMessage, ...] | None,
        *,
        user_turn_override: UserMessage | None = None,
    ) -> UserTurn | EndRollout:
        if user_turn_override is not None:
            if self._messages:
                self._messages.pop(0)
            return UserTurn(message=user_turn_override)
        if not self._messages:
            return EndRollout()
        return UserTurn(message=self._messages.pop(0))

    async def get_state(self) -> DynamicData:
        return DynamicData.from_tagged(_ScriptedState(messages=tuple(self._messages)))

    async def aclose(self) -> None:
        pass


@dataclasses.dataclass(frozen=True)
class ScriptedUserSimulator:
    """Sends the given user messages in order, then ends the rollout.

    With a single message and a tool policy, this runs one prompt until the
    model stops making tool calls (the simulator is only consulted on
    non-tool turns, so its second consultation ends the rollout); with no
    messages, it ends the rollout at the first consultation. A
    ``user_turn_override`` is sent in place of the scripted message that was
    next, which is dropped. Session state is the messages not yet sent.

    Attributes:
        messages: The user messages to send, one per consultation.
    """

    messages: tuple[UserMessage, ...]

    async def start_session(self) -> SerializableUserSimulatorSession:
        return _ScriptedUserSimulatorSession(self.messages)

    async def restore_session(self, state: DynamicData) -> SerializableUserSimulatorSession:
        return _ScriptedUserSimulatorSession(state.as_type(_ScriptedState).messages)
