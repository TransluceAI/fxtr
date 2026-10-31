"""Context policies built from a single component.

``context_policy_from_user_simulator`` runs a plain chat: the user simulator
writes every non-model message and decides when the rollout ends; no tools
are ever offered, and a model turn that contains tool calls anyway is an
error. ``context_policy_from_tool_policy`` runs an agentic loop: a fixed
first turn plus the policy's tools, every model turn's tool calls answered,
and the rollout ends when the model produces a turn with no tool calls (cap
runaway loops with ``run_chat_rollout``'s ``max_turns``).
"""

from behaviors.implementations.context_policies._shared import tool_uses_in
from behaviors.interfaces.context_policies import (
    ContextPolicy,
    SerializableContextPolicy,
    SerializableToolPolicy,
    SerializableToolPolicySession,
    SerializableUserSimulator,
    SerializableUserSimulatorSession,
    ToolPolicy,
    ToolPolicySession,
    UserSimulator,
    UserSimulatorSession,
)
from behaviors.types.message import ChatMessage
from behaviors.types.rollout import ContextTurn, EndRollout, ModelTurn, SessionInitialization
from fxtr.entity_defns.dynamic_data import DynamicData


class _UserSimulatorPolicySession:
    def __init__(
        self,
        user_session: UserSimulatorSession,
        initialization: SessionInitialization | None,
    ):
        self._user_session = user_session
        self._initialization = initialization

    async def generate_initialization(self) -> SessionInitialization | None:
        return self._initialization

    async def generate_context_turn(self, model_turn: ModelTurn | None) -> ContextTurn | EndRollout:
        if model_turn is not None and tool_uses_in(model_turn):
            raise RuntimeError(
                "the model produced tool calls, but a user-simulator context policy"
                " never offers tools"
            )
        step = await self._user_session.next_user_turn(
            model_turn.messages if model_turn is not None else None
        )
        if isinstance(step, EndRollout):
            return step
        return ContextTurn(
            messages=(step.message,),
            available_tools=None,
            metadata=step.metadata,
        )

    async def aclose(self) -> None:
        await self._user_session.aclose()


class _SerializableUserSimulatorPolicySession(_UserSimulatorPolicySession):
    def __init__(
        self,
        user_session: SerializableUserSimulatorSession,
        initialization: SessionInitialization | None,
    ):
        super().__init__(user_session, initialization)
        self._serializable_user_session = user_session

    async def get_state(self) -> DynamicData:
        return await self._serializable_user_session.get_state()


class _UserSimulatorPolicy:
    def __init__(self, user_simulator: UserSimulator, initialization: SessionInitialization | None):
        self._user_simulator = user_simulator
        self._initialization = initialization

    async def start_session(self) -> _UserSimulatorPolicySession:
        return _UserSimulatorPolicySession(
            await self._user_simulator.start_session(), self._initialization
        )


class _SerializableUserSimulatorPolicy:
    def __init__(
        self,
        user_simulator: SerializableUserSimulator,
        initialization: SessionInitialization | None,
    ):
        self._user_simulator = user_simulator
        self._initialization = initialization

    async def start_session(self) -> _SerializableUserSimulatorPolicySession:
        return _SerializableUserSimulatorPolicySession(
            await self._user_simulator.start_session(), self._initialization
        )

    async def restore_session(self, state: DynamicData) -> _SerializableUserSimulatorPolicySession:
        return _SerializableUserSimulatorPolicySession(
            await self._user_simulator.restore_session(state), self._initialization
        )


def context_policy_from_user_simulator(
    user_simulator: UserSimulator,
    *,
    initialization: SessionInitialization | None,
) -> ContextPolicy:
    """Lift a user simulator into a context policy for tool-less chat.

    Every context turn is one user message; the simulator's turn metadata is
    copied onto the context turn it produces, and its EndRollout is recorded
    unchanged. Tools are never offered, and a model turn containing tool
    calls raises rather than being passed along.
    """
    return _UserSimulatorPolicy(user_simulator, initialization)


def serializable_context_policy_from_user_simulator(
    user_simulator: SerializableUserSimulator,
    *,
    initialization: SessionInitialization | None,
) -> SerializableContextPolicy:
    """Like ``context_policy_from_user_simulator``, with serializable state.

    The policy session's state is exactly the user simulator session's state.
    """
    return _SerializableUserSimulatorPolicy(user_simulator, initialization)


class _ToolLoopPolicySession:
    def __init__(
        self,
        tool_session: ToolPolicySession,
        initialization: SessionInitialization | None,
        first_turn_messages: tuple[ChatMessage, ...],
    ):
        self._tool_session = tool_session
        self._initialization = initialization
        self._first_turn_messages = first_turn_messages

    async def generate_initialization(self) -> SessionInitialization | None:
        return self._initialization

    async def generate_context_turn(self, model_turn: ModelTurn | None) -> ContextTurn | EndRollout:
        if model_turn is None:
            return ContextTurn(
                messages=self._first_turn_messages,
                available_tools=await self._tool_session.available_tools(),
            )
        tool_uses = tool_uses_in(model_turn)
        if not tool_uses:
            return EndRollout()
        tool_turn = await self._tool_session.generate_tool_turn(tool_uses)
        return ContextTurn(
            messages=tool_turn.messages,
            available_tools=await self._tool_session.available_tools(),
            metadata=tool_turn.metadata,
        )

    async def aclose(self) -> None:
        await self._tool_session.aclose()


class _SerializableToolLoopPolicySession(_ToolLoopPolicySession):
    def __init__(
        self,
        tool_session: SerializableToolPolicySession,
        initialization: SessionInitialization | None,
        first_turn_messages: tuple[ChatMessage, ...],
    ):
        super().__init__(tool_session, initialization, first_turn_messages)
        self._serializable_tool_session = tool_session

    async def get_state(self) -> DynamicData:
        return await self._serializable_tool_session.get_state()


class _ToolLoopPolicy:
    def __init__(
        self,
        tool_policy: ToolPolicy,
        initialization: SessionInitialization | None,
        first_turn_messages: tuple[ChatMessage, ...],
    ):
        self._tool_policy = tool_policy
        self._initialization = initialization
        self._first_turn_messages = first_turn_messages

    async def start_session(self) -> _ToolLoopPolicySession:
        return _ToolLoopPolicySession(
            await self._tool_policy.start_session(), self._initialization, self._first_turn_messages
        )


class _SerializableToolLoopPolicy:
    def __init__(
        self,
        tool_policy: SerializableToolPolicy,
        initialization: SessionInitialization | None,
        first_turn_messages: tuple[ChatMessage, ...],
    ):
        self._tool_policy = tool_policy
        self._initialization = initialization
        self._first_turn_messages = first_turn_messages

    async def start_session(self) -> _SerializableToolLoopPolicySession:
        return _SerializableToolLoopPolicySession(
            await self._tool_policy.start_session(), self._initialization, self._first_turn_messages
        )

    async def restore_session(self, state: DynamicData) -> _SerializableToolLoopPolicySession:
        return _SerializableToolLoopPolicySession(
            await self._tool_policy.restore_session(state),
            self._initialization,
            self._first_turn_messages,
        )


def context_policy_from_tool_policy(
    tool_policy: ToolPolicy,
    *,
    initialization: SessionInitialization | None,
    first_turn_messages: tuple[ChatMessage, ...],
) -> ContextPolicy:
    """Lift a tool policy into a context policy for an agentic loop.

    The first context turn is ``first_turn_messages`` with the policy's tools;
    afterwards every model turn's tool calls are answered by the tool policy
    (each tool turn's metadata is copied onto the context turn it produces),
    and a model turn with no tool calls ends the rollout with a plain
    ``EndRollout``.
    """
    return _ToolLoopPolicy(tool_policy, initialization, first_turn_messages)


def serializable_context_policy_from_tool_policy(
    tool_policy: SerializableToolPolicy,
    *,
    initialization: SessionInitialization | None,
    first_turn_messages: tuple[ChatMessage, ...],
) -> SerializableContextPolicy:
    """Like ``context_policy_from_tool_policy``, with serializable state.

    The policy session's state is exactly the tool policy session's state.
    """
    return _SerializableToolLoopPolicy(tool_policy, initialization, first_turn_messages)
