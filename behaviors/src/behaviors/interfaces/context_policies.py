"""Context policy protocols: everything in a rollout that is not the model.

A context policy is the chat-session-model analog for non-assistant messages:
its session generates what the model is shown next (user messages, tool
results, available tools) in response to each model turn, and decides when
the rollout ends by returning an ``EndRollout`` (with end-of-rollout
information in its metadata) instead of a turn. ``UserSimulator`` and
``ToolPolicy`` are narrower component interfaces: a user simulator only
writes user messages and may also end the rollout; a tool policy is a pure
supplier of tool results and cannot end the rollout — an environment that
owns termination (a submit tool reaching a terminal state, a verifier) is a
full context policy, not a tool policy.
``behaviors.implementations.context_policies`` provides first-party lifts
from either component to a full context policy, plus one way of combining
the two; other combinations are written by their users.
"""

from typing import Protocol, runtime_checkable

from behaviors.types.content import ContentToolUse
from behaviors.types.message import ChatMessage, UserMessage
from behaviors.types.rollout import (
    ContextTurn,
    EndRollout,
    ModelTurn,
    SessionInitialization,
    ToolTurn,
    UserTurn,
)
from behaviors.types.tool import ToolParam
from fxtr.entity_defns.dynamic_data import DynamicData


@runtime_checkable
class ContextPolicySession(Protocol):
    """One rollout's worth of context generation."""

    async def generate_initialization(self) -> SessionInitialization | None:
        """Return the initialization for the model's session.

        The context policy owns the scenario, so it also owns how the model's
        session starts (e.g. the system prompt); the returned value is what
        gets passed to ``ChatSessionModel.start_session``. Implementations may
        assume this is called at most once, before any
        ``generate_context_turn`` call.
        """
        ...

    async def generate_context_turn(self, model_turn: ModelTurn | None) -> ContextTurn | EndRollout:
        """Return the next context turn, or an EndRollout to end the rollout.

        Args:
            model_turn: The model's latest turn, or None if the model has not
                taken a turn yet. Model call failures are never passed in;
                implementations need not handle them (the rollout runner
                does).
        """
        ...

    async def aclose(self) -> None:
        """Release the session's resources; the session is unusable afterwards.

        A no-op if there is nothing to release. Implementations may assume it
        is called exactly once, even after the session ended the rollout
        itself. The name makes sessions compatible with
        ``contextlib.aclosing``.
        """
        ...


@runtime_checkable
class ContextPolicy(Protocol):
    """A factory for context policy sessions, one per rollout."""

    async def start_session(self) -> ContextPolicySession:
        """Return a fresh session, independent of any other session."""
        ...


@runtime_checkable
class SerializableContextPolicySession(ContextPolicySession, Protocol):
    """A context policy session whose state can be captured.

    The state contract matches ``SerializableChatSession.get_state``.
    """

    async def get_state(self) -> DynamicData: ...


@runtime_checkable
class SerializableContextPolicy(ContextPolicy, Protocol):
    """A context policy whose sessions have serializable state."""

    async def start_session(self) -> SerializableContextPolicySession: ...

    async def restore_session(self, state: DynamicData) -> SerializableContextPolicySession:
        """Start a session that continues from previously captured state."""
        ...


@runtime_checkable
class UserSimulatorSession(Protocol):
    """One rollout's worth of simulated user messages."""

    async def next_user_turn(
        self,
        model_messages: tuple[ChatMessage, ...] | None,
        *,
        user_turn_override: UserMessage | None = None,
    ) -> UserTurn | EndRollout:
        """Return the next user turn, or an EndRollout to end the rollout.

        Args:
            model_messages: The messages the model has produced since the
                previous call, already filtered to what the simulated user
                should see — or None if the user speaks first.
            user_turn_override: A user message chosen outside the simulator
                (e.g. by a pilot steering it) to send as this turn in place of
                one the simulator would have produced. The session records it
                in its history exactly as it would its own turn. Simulators
                that cannot take an externally chosen turn raise
                ``NotImplementedError``.
        """
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class UserSimulator(Protocol):
    """A factory for user simulator sessions, one per rollout."""

    async def start_session(self) -> UserSimulatorSession: ...


@runtime_checkable
class SerializableUserSimulatorSession(UserSimulatorSession, Protocol):
    """A user simulator session whose state can be captured.

    The state contract matches ``SerializableChatSession.get_state``.
    """

    async def get_state(self) -> DynamicData: ...


@runtime_checkable
class SerializableUserSimulator(UserSimulator, Protocol):
    """A user simulator whose sessions have serializable state."""

    async def start_session(self) -> SerializableUserSimulatorSession: ...

    async def restore_session(self, state: DynamicData) -> SerializableUserSimulatorSession:
        """Start a session that continues from previously captured state."""
        ...


@runtime_checkable
class ToolPolicySession(Protocol):
    """One rollout's worth of simulated tool results."""

    async def available_tools(self) -> tuple[ToolParam, ...]:
        """The tools to offer the model on the next turn."""
        ...

    async def generate_tool_turn(self, tool_uses: tuple[ContentToolUse, ...]) -> ToolTurn:
        """Produce one tool result per tool use, in order.

        A tool policy cannot end the rollout. One backed by an environment
        with terminal states can record them on the turn's metadata for the
        enclosing context policy to act on.
        """
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class ToolPolicy(Protocol):
    """A factory for tool policy sessions, one per rollout."""

    async def start_session(self) -> ToolPolicySession: ...


@runtime_checkable
class SerializableToolPolicySession(ToolPolicySession, Protocol):
    """A tool policy session whose state can be captured.

    The state contract matches ``SerializableChatSession.get_state``.
    """

    async def get_state(self) -> DynamicData: ...


@runtime_checkable
class SerializableToolPolicy(ToolPolicy, Protocol):
    """A tool policy whose sessions have serializable state."""

    async def start_session(self) -> SerializableToolPolicySession: ...

    async def restore_session(self, state: DynamicData) -> SerializableToolPolicySession:
        """Start a session that continues from previously captured state."""
        ...
