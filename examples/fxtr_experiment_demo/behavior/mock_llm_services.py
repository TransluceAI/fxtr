"""Mocked LLM services for the behavior scenario's ``mock`` mode.

Fake implementations of the *same three protocols* the real
Anthropic-backed services implement, bound under **distinct handle
keys** (``behavior_research_types.keys``: the ``*_MOCK_KEY``\\ s), so
mock-computed results can never be observably confused with real ones
— switching mode rewrites the handle cells and fans staleness through
everything (see the keys module docstring).

Everything is deterministic and *obviously* fake: replies are tagged
``[MOCK ...]``, and the judge's score is a stable hash of
(rubric, transcript) — reproducible, but visibly varied across
(subject, judge) pairs so the analysis table has texture. The
"flavor" fragments derive from the config prompts, so different
subject models / personas produce different transcripts (and thus
different judge scores).
"""

from __future__ import annotations

import hashlib
from typing import Any

from behavior_research_types import (
    JUDGE_MODEL_TYPE_ID,
    SUBJECT_MODEL_TYPE_ID,
    USER_SIM_TYPE_ID,
    JudgeModelProto,
    SubjectModelProto,
    UserSimProto,
)
from fxtr2.data.entity_core import FxtrID
from fxtr2.services.service_cap_common import FxtrServiceData
from fxtr2.services.typed_service import TypedServiceImpl
from fxtr2.services.untyped_service import service_method


def _flavor(text: str) -> str:
    """A short stable tag derived from ``text`` (prompt → visible variety)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]


class MockSubjectModel(TypedServiceImpl[SubjectModelProto]):
    """Canned assistant turns, varying with system prompt + turn index.

    Also stands in for the **improver** (the sim-improvement flow
    reuses the subject-model handle for its rewrite calls): when the
    system prompt asks for the ``<persona>`` reply format, the mock
    speaks that contract — a deterministic, visibly fake
    ``<notes>/<persona>`` reply varied by the message content, so the
    flow's iterations produce distinct candidates. Prompt-format
    sniffing, deliberately: the mock must satisfy the real parser, not
    the other way around.
    """

    def _target_type_annotation(self) -> Any:
        return SubjectModelProto

    def _service_type_id(self) -> FxtrID:
        return SUBJECT_MODEL_TYPE_ID

    @service_method
    async def reply(self, system_prompt: str, model: str, messages: list[FxtrServiceData]) -> str:
        if "<persona>" in system_prompt:
            variant = _flavor("\n".join(str(m["text"]) for m in messages))
            return (
                f"<notes>[MOCK improver notes {variant}] Nudged the persona per "
                "the instruction; not a real analysis.</notes>\n"
                f"<persona>[MOCK improved persona {variant}] A customer who read "
                "the instruction and now behaves accordingly.</persona>"
            )
        turn = sum(1 for m in messages if m["role"] == "assistant") + 1
        return (
            f"[MOCK assistant {_flavor(system_prompt)} turn {turn}] Speaking as "
            f"{model!r}: this hatchback practically sells itself."
        )


class MockUserSim(TypedServiceImpl[UserSimProto]):
    """Canned user turns, varying with persona + turn index."""

    def _target_type_annotation(self) -> Any:
        return UserSimProto

    def _service_type_id(self) -> FxtrID:
        return USER_SIM_TYPE_ID

    @service_method
    async def next_message(
        self, task_prompt: str, persona: str, messages: list[FxtrServiceData]
    ) -> str:
        turn = sum(1 for m in messages if m["role"] == "user") + 1
        return (
            f"[MOCK user {_flavor(persona)} turn {turn}] So, tell me more "
            "about this car you keep mentioning."
        )


class MockJudge(TypedServiceImpl[JudgeModelProto]):
    """Deterministic hash-derived verdicts (visibly fake, reproducible)."""

    def _target_type_annotation(self) -> Any:
        return JudgeModelProto

    def _service_type_id(self) -> FxtrID:
        return JUDGE_MODEL_TYPE_ID

    @service_method
    async def judge(
        self, rubric: str, model: str, messages: list[FxtrServiceData]
    ) -> FxtrServiceData:
        transcript_text = "\n".join(f"{m['role']}: {m['text']}" for m in messages)
        digest = hashlib.sha256((rubric + "\0" + transcript_text).encode("utf-8")).hexdigest()
        score = (int(digest[:8], 16) % 101) / 100
        return {
            "score": score,
            "reasoning": (
                f"MOCK VERDICT ({model}): score {score:.2f} is a deterministic "
                "hash of (rubric, transcript) — not a real judgement."
            ),
        }
