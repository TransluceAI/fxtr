"""The one-citation format both experiments use for a field that points at one place."""

import pytest

from behavior_rollouts.transcript_judging import TRANSCRIPT, one_line_citation_validator
from behaviors.types import AssistantMessage, ContentText, Transcript, UserMessage

pytestmark = pytest.mark.anyio


TRANSCRIPT_ENTITY = Transcript(
    name=None,
    description=None,
    messages=(
        UserMessage(content=(ContentText(text="Never say party."),)),
        AssistantMessage(content=(ContentText(text="Understood.\nNo parties here."),)),
    ),
)


@pytest.mark.parametrize("value", ["", "  ", "[transcript:B1L1]", "[transcript:B1L0-1]"])
async def test_one_line_citation_accepts_one_citation_of_lines_or_nothing(value: str) -> None:
    assert one_line_citation_validator({TRANSCRIPT: TRANSCRIPT_ENTITY}, value) is None


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("[transcript:B1L0] [transcript:B1L1]", "exactly one citation"),
        ("See [transcript:B1L1]", "exactly one citation"),
        ("[transcript:B1]", "exactly one citation"),
        ("[transcript:B1L5]", "L5"),
    ],
)
async def test_one_line_citation_rejects_anything_else(value: str, problem: str) -> None:
    message = one_line_citation_validator({TRANSCRIPT: TRANSCRIPT_ENTITY}, value)
    assert message is not None and problem in message
