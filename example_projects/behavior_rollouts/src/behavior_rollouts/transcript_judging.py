"""Judging a conversation's transcript, as both experiments do: the judge step and what it shares.

``judge_transcript`` is the step both workflows schedule. It calls behaviors' ``judge`` on one
``Transcript`` with an experiment's rubric and output schema, which it takes as stored inputs, and
returns the ``Judgment``. Judges differ only in their model and its sampling (``models.py``), so
their agreement and disagreement are about the same question.

The judge is given the transcript under the name ``TRANSCRIPT``, and its instructions are
behaviors' defaults (``JUDGE_INSTRUCTIONS``). The transcript is written by behaviors' line
citation format, so the judge cites it by line (``[transcript:B3L0]``). An output schema's fields
use the formats in ``CUSTOM_FORMATS``:

- ``AGENT_RUN_LINE_CITATION_FORMAT``: prose with a citation of the transcript's lines after each
  claim.
- ``ONE_LINE_CITATION_FORMAT``: exactly one citation of the transcript's lines, or an empty string.
"""

import dataclasses
import json
import re
from collections.abc import Mapping
from typing import cast

import anyio

from behavior_rollouts.models import SamplingParamsEntity, make_api
from behaviors.judging.agent_run_evidence import (
    AGENT_RUN_LINE_CITATION_FORMAT,
    agent_run_line_citation_validator,
)
from behaviors.judging.defaults import (
    DEFAULT_ENTITY_TO_PROMPT_TEXT,
    DEFAULT_SYSTEM_INSTRUCTIONS_ENTITY,
)
from behaviors.judging.judge import CustomFormat, Judgment, judge
from behaviors.types import (
    ContentText,
    SystemMessage,
    TextEntity,
    Transcript,
    UserMessage,
)
from behaviors.types.json import JSONObject, JSONValue
from fxtr.core.entities import Entity
from fxtr.experiment.steps import StepContext, step

TRANSCRIPT = "transcript"
"""The name the judge is given the conversation under, and cites it by."""

JUDGE_INSTRUCTIONS = DEFAULT_SYSTEM_INSTRUCTIONS_ENTITY
"""The judge's instructions: behaviors' defaults.

``AGENT_RUN_LINE_CITATION_FORMAT`` adds the text that explains the transcript's line labels.
"""

CONTEXT_TEMPLATE = (
    SystemMessage(content=(ContentText(text="{instructions}\n\n{citation_instructions}"),)),
    UserMessage(
        content=(ContentText(text=f"{{{TRANSCRIPT}}}\n\n{{rubric}}\n\n{{output_schema}}"),)
    ),
)

_ONE_LINE_CITATION = re.compile(
    rf"\[{TRANSCRIPT}:B(?:0|[1-9][0-9]*)L(?:0|[1-9][0-9]*)(?:-L?(?:0|[1-9][0-9]*))?\]"
)


def one_line_citation_validator(entities: Mapping[str, Entity], value: JSONValue) -> str | None:
    """Accept an empty string, or exactly one citation of lines of the transcript that exist."""
    if not isinstance(value, str) or value.strip() == "":
        return None
    if _ONE_LINE_CITATION.fullmatch(value.strip()) is None:
        return (
            f"Write exactly one citation of the {TRANSCRIPT}'s lines, such as"
            f" [{TRANSCRIPT}:B4L0], and nothing else; or an empty string when there is none."
        )
    return agent_run_line_citation_validator(entities, value)


ONE_LINE_CITATION_FORMAT = dataclasses.replace(
    AGENT_RUN_LINE_CITATION_FORMAT,
    name="line_citation",
    validator=one_line_citation_validator,
)
"""The format of a field that points at one place: one citation of the transcript's lines, or
nothing.

It writes and describes the transcript as ``AGENT_RUN_LINE_CITATION_FORMAT`` does, so the two
formats can be used in one answer.
"""

CUSTOM_FORMATS: tuple[CustomFormat, ...] = (
    AGENT_RUN_LINE_CITATION_FORMAT,
    ONE_LINE_CITATION_FORMAT,
)
"""Every custom format the experiments' output schemas use, to pass to ``judge``."""


@step(name="transcript_judging.judge_transcript")
async def judge_transcript(
    context: StepContext,
    model_name: str,
    sampling: SamplingParamsEntity,
    instructions: TextEntity,
    rubric: TextEntity,
    output_schema: TextEntity,
    transcript: Transcript,
) -> Judgment:
    """Judge one transcript against the rubric, answering in the output schema.

    Every input is stored, so the cache sees each part of the request. The judgment refers to the
    instructions, rubric and transcript by ID. ``output_schema`` is the answer's JSON Schema, as
    JSON text.
    """
    api = make_api(model_name)
    try:
        judgment = await judge(
            api,
            context_template=CONTEXT_TEMPLATE,
            entities={"instructions": instructions, "rubric": rubric, TRANSCRIPT: transcript},
            entity_to_prompt_text=DEFAULT_ENTITY_TO_PROMPT_TEXT,
            output_schema=cast(JSONObject, json.loads(output_schema.text)),
            sampling=sampling.params,
            custom_formats=CUSTOM_FORMATS,
        )
    finally:
        with anyio.CancelScope(shield=True):
            await api.aclose()
    answers = len(judgment.attempts)
    if judgment.output is None:
        context.log(f"no answer accepted after {answers} answers")
    else:
        verdict = {key: value for key, value in judgment.output.items() if key != "discussion"}
        context.log(f"{verdict} after {answers} answers")
    return judgment
