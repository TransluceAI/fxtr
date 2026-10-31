"""Combining a user simulator and a tool policy into a context policy.

This is one reasonable combination, not the only one: model turns with tool
calls are answered by the tool policy, everything else by the user simulator.
Experiments that need different sequencing or visibility rules (the user
interjecting mid-tool-loop, summaries of tool activity, tools offered
conditionally) should write their own context policy, using this module as a
reference.
"""

import dataclasses

import anyio

from behaviors.implementations.context_policies._shared import strip_tool_uses, tool_uses_in
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
from behaviors.types.namespace import behaviors_type
from behaviors.types.rollout import ContextTurn, EndRollout, ModelTurn, SessionInitialization
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData


@dataclasses.dataclass(frozen=True)
class _CombinedState(TaggedDataclass, fxtr_type=behaviors_type("UserAndToolsState.v1")):
    """A combined session's state: its components' states plus its own."""

    user_state: DynamicData
    tool_state: DynamicData
    pending_for_user: tuple[ChatMessage, ...]
    started: bool


class _CombinedContextPolicySession:
    def __init__(
        self,
        user_session: UserSimulatorSession,
        tool_session: ToolPolicySession,
        initialization: SessionInitialization | None,
        show_tool_calls_to_user: bool,
        pending_for_user: tuple[ChatMessage, ...] = (),
        started: bool = False,
    ):
        self._user_session = user_session
        self._tool_session = tool_session
        self._initialization = initialization
        self._show_tool_calls_to_user = show_tool_calls_to_user
        self._pending_for_user = pending_for_user
        self._started = started

    async def generate_initialization(self) -> SessionInitialization | None:
        return self._initialization

    async def generate_context_turn(self, model_turn: ModelTurn | None) -> ContextTurn | EndRollout:
        if model_turn is not None:
            visible = (
                model_turn.messages
                if self._show_tool_calls_to_user
                else strip_tool_uses(model_turn.messages)
            )
            self._pending_for_user += visible

            tool_uses = tool_uses_in(model_turn)
            if tool_uses:
                tool_turn = await self._tool_session.generate_tool_turn(tool_uses)
                if self._show_tool_calls_to_user:
                    self._pending_for_user += tool_turn.messages
                return ContextTurn(
                    messages=tool_turn.messages,
                    available_tools=await self._tool_session.available_tools(),
                    metadata=tool_turn.metadata,
                )

        pending, self._pending_for_user = self._pending_for_user, ()
        step = await self._user_session.next_user_turn(pending if self._started else None)
        self._started = True
        if isinstance(step, EndRollout):
            return step
        return ContextTurn(
            messages=(step.message,),
            available_tools=await self._tool_session.available_tools(),
            metadata=step.metadata,
        )

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            try:
                await self._user_session.aclose()
            finally:
                await self._tool_session.aclose()


class _CombinedContextPolicy:
    def __init__(
        self,
        user_simulator: UserSimulator,
        tool_policy: ToolPolicy,
        initialization: SessionInitialization | None,
        show_tool_calls_to_user: bool,
    ):
        self._user_simulator = user_simulator
        self._tool_policy = tool_policy
        self._initialization = initialization
        self._show_tool_calls_to_user = show_tool_calls_to_user

    async def start_session(self) -> _CombinedContextPolicySession:
        user_session = await self._user_simulator.start_session()
        try:
            tool_session = await self._tool_policy.start_session()
        except BaseException:
            with anyio.CancelScope(shield=True):
                await user_session.aclose()
            raise
        return _CombinedContextPolicySession(
            user_session,
            tool_session,
            self._initialization,
            self._show_tool_calls_to_user,
        )


class _SerializableCombinedContextPolicySession(_CombinedContextPolicySession):
    def __init__(
        self,
        user_session: SerializableUserSimulatorSession,
        tool_session: SerializableToolPolicySession,
        initialization: SessionInitialization | None,
        show_tool_calls_to_user: bool,
        pending_for_user: tuple[ChatMessage, ...] = (),
        started: bool = False,
    ):
        super().__init__(
            user_session,
            tool_session,
            initialization,
            show_tool_calls_to_user,
            pending_for_user,
            started,
        )
        self._serializable_user_session = user_session
        self._serializable_tool_session = tool_session

    async def get_state(self) -> DynamicData:
        return DynamicData.from_tagged(
            _CombinedState(
                user_state=await self._serializable_user_session.get_state(),
                tool_state=await self._serializable_tool_session.get_state(),
                pending_for_user=self._pending_for_user,
                started=self._started,
            )
        )


class _SerializableCombinedContextPolicy:
    def __init__(
        self,
        user_simulator: SerializableUserSimulator,
        tool_policy: SerializableToolPolicy,
        initialization: SessionInitialization | None,
        show_tool_calls_to_user: bool,
    ):
        self._user_simulator = user_simulator
        self._tool_policy = tool_policy
        self._initialization = initialization
        self._show_tool_calls_to_user = show_tool_calls_to_user

    async def start_session(self) -> _SerializableCombinedContextPolicySession:
        user_session = await self._user_simulator.start_session()
        try:
            tool_session = await self._tool_policy.start_session()
        except BaseException:
            with anyio.CancelScope(shield=True):
                await user_session.aclose()
            raise
        return _SerializableCombinedContextPolicySession(
            user_session,
            tool_session,
            self._initialization,
            self._show_tool_calls_to_user,
        )

    async def restore_session(
        self, state: DynamicData
    ) -> _SerializableCombinedContextPolicySession:
        decoded = state.as_type(_CombinedState)
        user_session = await self._user_simulator.restore_session(decoded.user_state)
        try:
            tool_session = await self._tool_policy.restore_session(decoded.tool_state)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await user_session.aclose()
            raise
        return _SerializableCombinedContextPolicySession(
            user_session,
            tool_session,
            self._initialization,
            self._show_tool_calls_to_user,
            pending_for_user=decoded.pending_for_user,
            started=decoded.started,
        )


def context_policy_from_user_and_tools(
    user_simulator: UserSimulator,
    tool_policy: ToolPolicy,
    *,
    initialization: SessionInitialization | None,
    show_tool_calls_to_user: bool = False,
) -> ContextPolicy:
    """Combine a user simulator and a tool policy into a context policy.

    Model turns containing tool calls are answered by the tool policy; all
    other turns are answered by the user simulator, which sees the model's
    messages accumulated since it was last consulted.
    ``show_tool_calls_to_user`` controls whether tool calls and tool results
    appear in what the simulated user sees. Only the user simulator can end
    the rollout; its EndRollout is recorded unchanged. Each emitted context
    turn copies the producing user or tool turn's metadata.
    """
    return _CombinedContextPolicy(
        user_simulator, tool_policy, initialization, show_tool_calls_to_user
    )


def serializable_context_policy_from_user_and_tools(
    user_simulator: SerializableUserSimulator,
    tool_policy: SerializableToolPolicy,
    *,
    initialization: SessionInitialization | None,
    show_tool_calls_to_user: bool = False,
) -> SerializableContextPolicy:
    """Like ``context_policy_from_user_and_tools``, with serializable state.

    Requires serializable components; the combined session state composes
    theirs with the combiner's own.
    """
    return _SerializableCombinedContextPolicy(
        user_simulator, tool_policy, initialization, show_tool_calls_to_user
    )
