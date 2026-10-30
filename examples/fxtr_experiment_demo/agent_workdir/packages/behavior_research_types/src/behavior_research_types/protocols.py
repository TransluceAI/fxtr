"""Service protocols for the behavior-research scenario's LLM services.

Three deliberately distinct protocols — each type id names a *specific
contract*, and the workflow's handle grids are typed by which contract
the bound service implements. Messages cross the wire as plain
``[{"role": ..., "text": ...}, ...]`` dicts (the wire form of
:class:`~behavior_research_types.entities.ChatMessage`), oldest first;
``role`` is ``"user"`` (the simulated user) or ``"assistant"`` (the
subject model), always from the *subject model's* perspective —
implementations that need the opposite orientation (the user
simulator) flip roles internally.
"""

from __future__ import annotations

import uuid
from typing import Protocol

from fxtr2.data import system_entities
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import service_protocol

#: Stable namespace UUID for the behavior-research service type ids.
BEHAVIOR_SERVICES_NS = uuid.UUID("f620242c-3699-4899-a402-ee6fc895bbe2")

SUBJECT_MODEL_TYPE_ID = system_entities.define_type(BEHAVIOR_SERVICES_NS, "subject_model_service")
USER_SIM_TYPE_ID = system_entities.define_type(BEHAVIOR_SERVICES_NS, "user_simulator_service")
JUDGE_MODEL_TYPE_ID = system_entities.define_type(BEHAVIOR_SERVICES_NS, "judge_model_service")


@service_protocol
class SubjectModelProto(Protocol):
    """A chat-completion service standing in for one model provider."""

    async def reply(self, system_prompt: str, model: str, messages: list[FxtrServiceData]) -> str:
        """The assistant's next message.

        ``messages`` is the conversation so far (module docstring wire
        shape); the last message has role ``"user"``. Returns the
        assistant's reply text.
        """
        ...


@service_protocol
class UserSimProto(Protocol):
    """The simulated-user side of a conversation."""

    async def next_message(
        self, task_prompt: str, persona: str, messages: list[FxtrServiceData]
    ) -> str:
        """The simulated user's next message.

        ``messages`` is the conversation so far (module docstring wire
        shape — subject-model perspective; empty on the opening turn,
        otherwise its last message has role ``"assistant"``). Returns
        the user's next message text.
        """
        ...


@service_protocol
class JudgeModelProto(Protocol):
    """Scores a finished transcript against a rubric."""

    async def judge(
        self, rubric: str, model: str, messages: list[FxtrServiceData]
    ) -> FxtrServiceData:
        """Judge the transcript.

        Returns ``{"score": float in [0, 1], "reasoning": str}`` —
        the wire form callers store as a
        :class:`~behavior_research_types.entities.JudgeResult`.
        """
        ...
