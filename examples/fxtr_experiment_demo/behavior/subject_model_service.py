"""The real subject-model service: chat completion via Anthropic.

Implements :class:`behavior_research_types.SubjectModelProto` — the
assistant side of a rollout. The impl is deliberately a passthrough:
system prompt and model come from the caller's
:class:`~behavior_research_types.SubjectModelConfig` (read by the
rollout task), so one bound service serves every subject config that
names its provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from behavior_research_types import SUBJECT_MODEL_TYPE_ID, SubjectModelProto
from fxtr2.data.entity_core import FxtrID
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method

if TYPE_CHECKING:
    from anthropic_client import AnthropicChat


class SubjectModelImpl(TypedServiceImpl[SubjectModelProto]):
    """See the module docstring."""

    def __init__(self, chat: "AnthropicChat") -> None:
        super().__init__()
        self._chat = chat

    def _target_type_annotation(self) -> Any:
        return SubjectModelProto

    def _service_type_id(self) -> FxtrID:
        return SUBJECT_MODEL_TYPE_ID

    @service_method
    async def reply(self, system_prompt: str, model: str, messages: list[FxtrServiceData]) -> str:
        """Contract on :meth:`SubjectModelProto.reply`."""
        return await self._chat.complete(
            model=model,
            system=system_prompt,
            messages=messages,  # already assistant-perspective, ends with "user"
        )
