"""A tool policy that offers no tools."""

import dataclasses

from behaviors.interfaces.context_policies import SerializableToolPolicySession
from behaviors.types.content import ContentText, ContentToolUse
from behaviors.types.message import ToolMessage
from behaviors.types.namespace import behaviors_type
from behaviors.types.rollout import ToolTurn
from behaviors.types.tool import ToolParam
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData


@dataclasses.dataclass(frozen=True)
class _NoToolsState(TaggedDataclass, fxtr_type=behaviors_type("NoToolsState.v1")):
    """The (empty) state of a ``NoTools`` session."""


class _NoToolsSession:
    async def available_tools(self) -> tuple[ToolParam, ...]:
        return ()

    async def generate_tool_turn(self, tool_uses: tuple[ContentToolUse, ...]) -> ToolTurn:
        return ToolTurn(
            messages=tuple(
                ToolMessage(
                    content=(
                        ContentText(
                            text=f"Tool '{tool_use.name}' does not exist: no tools are available."
                        ),
                    ),
                    tool_call_id=tool_use.id,
                    is_error=True,
                )
                for tool_use in tool_uses
            )
        )

    async def get_state(self) -> DynamicData:
        return DynamicData.from_tagged(_NoToolsState())

    async def aclose(self) -> None:
        pass


@dataclasses.dataclass(frozen=True)
class NoTools:
    """A tool policy with no available tools.

    For combined policies that want tool-less behavior. A model that calls a
    tool anyway (there are none to call) gets an error result per call rather
    than crashing the rollout — the hallucinated call is behavior worth
    keeping. Sessions are stateless, so their state is an empty tagged value.
    """

    async def start_session(self) -> SerializableToolPolicySession:
        return _NoToolsSession()

    async def restore_session(self, state: DynamicData) -> SerializableToolPolicySession:
        state.as_type(_NoToolsState)
        return _NoToolsSession()
