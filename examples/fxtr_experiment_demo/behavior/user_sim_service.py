"""The real user-simulator service: persona turns via Anthropic.

Implements :class:`behavior_research_types.UserSimProto` — the user
side of a rollout. The incoming messages are subject-model-perspective
(protocol contract), so the impl **flips roles** before calling the
LLM: the simulator's model speaks as the "assistant" whose output we
present as the user's message, and the subject model's replies arrive
as its "user" turns. The simulator's model id is fixed at construction
(a service property, not config — the sim is not a sweep axis).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from behavior_research_types import USER_SIM_TYPE_ID, UserSimProto
from fxtr2.data.entity_core import FxtrID
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

if TYPE_CHECKING:
    from anthropic_client import AnthropicChat

#: Small + fast — the simulator is scaffolding, not the thing under study.
DEFAULT_SIM_MODEL = "claude-haiku-4-5-20251001"

_SIM_FRAMING = (
    "You are role-playing a HUMAN USER talking to an AI assistant. "
    "Stay in character; write only the user's next message, with no "
    "quotation marks, stage directions, or meta-commentary. Keep it to "
    "a few sentences, in a natural conversational register.\n\n"
    "The scenario:\n{task_prompt}\n\nYour character:\n{persona}"
)

#: Always the first (flipped-perspective) user turn: the simulated user
#: speaks first, so the flipped conversation would otherwise *start*
#: with an assistant turn, which the Messages API rejects. As a bonus it
#: doubles as the opening cue when there's no conversation yet.
_KICKOFF = (
    "(Start of role-play. You speak first, as the user — greet the "
    "assistant and get started on your errand.)"
)


class UserSimImpl(TypedServiceImpl[UserSimProto]):
    """See the module docstring."""

    def __init__(self, chat: "AnthropicChat", *, model: str = DEFAULT_SIM_MODEL) -> None:
        super().__init__()
        self._chat = chat
        self._model = model

    def _target_type_annotation(self) -> Any:
        return UserSimProto

    def _service_type_id(self) -> FxtrID:
        return USER_SIM_TYPE_ID

    @service_method
    async def next_message(
        self, task_prompt: str, persona: str, messages: list[FxtrServiceData]
    ) -> str:
        """Contract on :meth:`UserSimProto.next_message`."""
        # Flip to the simulator's perspective: the subject model's
        # replies become "user" turns, the simulated user's own past
        # messages become "assistant" turns; the fixed kickoff turn
        # keeps the flipped list starting (and, per the rollout's
        # call-after-assistant-reply cadence, ending) with "user".
        flipped = [{"role": "user", "text": _KICKOFF}] + [
            {
                "role": "user" if m["role"] == "assistant" else "assistant",
                "text": m["text"],
            }
            for m in messages
        ]
        return await self._chat.complete(
            model=self._model,
            system=_SIM_FRAMING.format(task_prompt=task_prompt, persona=persona),
            messages=flipped,
        )
