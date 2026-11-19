"""Judging agent runs and transcripts: how they are written as prompt text, described and cited.

An agent run or a transcript is cited by line. A line citation says where in a long message a
claim rests.

Citing by line takes three parts that have to agree with each other:

- ``agent_run_to_prompt_text`` and ``transcript_to_prompt_text`` write the entity as the text
  the judge model reads. They put a label in front of each message block and each line. ``T0B1``
  is a block in an agent run, and ``B1`` is a block in a transcript given by itself. ``L0``,
  ``L1`` and so on are the lines within a block. ``message_lines`` says what counts as a line.
- ``AGENT_RUN_LINES_FORMAT_INSTRUCTIONS_TEXT`` and ``TRANSCRIPT_LINES_FORMAT_INSTRUCTIONS_TEXT``
  explain that text to the model. For example, ``T0B3L4`` is a line and ``T0B3L4-6`` is a run of
  lines.
- ``agent_run_line_citation_validator`` checks the citations. Each one has to cite lines of an
  agent run or a transcript that exist. Its docstring lists what it sends back.

``AGENT_RUN_LINE_CITATION_FORMAT`` is the ``CustomFormat`` that holds the three. To use it,
write a citation field into the output schema with its ``schema`` method, and pass it to
``judge`` in ``custom_formats``.

A module for judging another kind of entity would have the same three parts, and a
``CustomFormat`` that holds them.
"""

import json
import re
from collections.abc import Mapping, Sequence

from behaviors.judging.citations import (
    check_citation_addresses,
    is_padded_address,
    malformed_citation_problems,
    misnamed_citation_problems,
    strings_in,
)
from behaviors.judging.judge import CustomFormat
from behaviors.types.agent_run import AgentRun, Transcript, TranscriptGroup
from behaviors.types.content import ContentText, ContentToolUse
from behaviors.types.json import JSONValue
from behaviors.types.message import ChatMessage
from fxtr.core.entities import Entity

_LINES_FORMAT_REST = """\
The line labels are not part of what was said. Each non-empty line of a message's text has one, and
so does each tool call, which is written on one line as <tool call>name({{...}})</tool call> with
its JSON arguments; a block with role: tool holds a tool result. The agent's thinking is not shown.
A line's address is its block's label followed by its own: {block}L0 is line L0 of block {block}.
A run of lines is written with a hyphen: {block}L0-1 is lines L0 to L1 of block {block}.

To cite a line, write the {entity}'s name, a colon and the line's address in square brackets, with
no spaces: [<name>:{block}L0], where <name> is the {entity}'s name. Every citation of the {entity}
must name a line or a run of lines. A block's label alone ({block}) is not accepted, and neither
is the {entity}'s name alone. In a field that asks for citations, cite only lines like these. Any
other citation there is not accepted, including a citation of another entity. Cite the fewest
lines that show the point.
"""

AGENT_RUN_LINES_FORMAT_INSTRUCTIONS_TEXT = """\
## Agent run format

An agent run is one or more transcripts, each a sequence of labelled message blocks, and each
line of a message begins with a label of its own, numbered from L0 within its block:

<|transcript T0|>
<|T0 blocks|>
<|T0B0; role: user|>
<|L0|>...first line of the first message...
</|T0B0; role: user|>
<|T0B1; role: assistant|>
<|L0|>...first line of the second message...

<|L1|>...second line of the second message...
</|T0B1; role: assistant|>
</|T0 blocks|>
</|transcript T0|>

T0B1 is the second message of the first transcript. Text between a transcript's opening tag and
its blocks describes that transcript.
""" + _LINES_FORMAT_REST.format(block="T0B1", entity="agent run")
"""The text that explains to the model what ``agent_run_to_prompt_text`` writes, and how to cite an
agent run by line.
"""

TRANSCRIPT_LINES_FORMAT_INSTRUCTIONS_TEXT = """\
## Transcript format

A transcript is a sequence of labelled message blocks, and each line of a message begins with a
label of its own, numbered from L0 within its block:

<|blocks|>
<|B0; role: user|>
<|L0|>...first line of the first message...
</|B0; role: user|>
<|B1; role: assistant|>
<|L0|>...first line of the second message...

<|L1|>...second line of the second message...
</|B1; role: assistant|>
</|blocks|>

B1 is the second message.
""" + _LINES_FORMAT_REST.format(block="B1", entity="transcript")
"""The text that explains to the model what ``transcript_to_prompt_text`` writes, and how to cite a
transcript by line.
"""


def agent_run_transcripts(agent_run: AgentRun) -> tuple[Transcript, ...]:
    """The transcripts of ``agent_run``, in the order they are written and labelled.

    The top-level transcripts come first. Each group's transcripts follow. A group comes before
    the groups nested in it. The transcript at position n is labelled ``T<n>``.
    """
    transcripts = list(agent_run.transcripts)

    def visit(group: TranscriptGroup) -> None:
        transcripts.extend(group.transcripts)
        for child in group.groups:
            visit(child)

    for group in agent_run.groups:
        visit(group)
    return tuple(transcripts)


def _tool_call_line(content: ContentToolUse) -> str:
    arguments = json.dumps(content.input, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"<tool call>{content.name}({arguments})</tool call>"


def message_lines(message: ChatMessage) -> list[str]:
    """The lines of ``message`` that can be cited.

    Line ``n`` in the result is ``L<n>`` of the message's block, as ``agent_run_to_prompt_text``
    and ``transcript_to_prompt_text`` label it.

    The message's content is read in order. Each non-empty line of a piece of text is a line.
    Each tool call is one line. Reasoning has no lines.
    """
    lines: list[str] = []
    for content in message.content:
        if isinstance(content, ContentText):
            lines.extend(line for line in content.text.split("\n") if line.strip())
        elif isinstance(content, ContentToolUse):
            lines.append(_tool_call_line(content))
    return lines


def _message_to_prompt_text(message: ChatMessage) -> str:
    pieces: list[str] = []
    number = 0
    for content in message.content:
        if isinstance(content, ContentText):
            for line in content.text.split("\n"):
                if line.strip():
                    pieces.append(f"<|L{number}|>{line}")
                    number += 1
                else:
                    pieces.append(line)
        elif isinstance(content, ContentToolUse):
            pieces.append(f"<|L{number}|>{_tool_call_line(content)}")
            number += 1
    return "\n".join(pieces)


def _blocks_to_prompt_text(messages: Sequence[ChatMessage], label_prefix: str) -> str:
    return "\n".join(
        f"<|{label_prefix}B{index}; role: {message.role}|>\n"
        f"{_message_to_prompt_text(message)}\n"
        f"</|{label_prefix}B{index}; role: {message.role}|>"
        for index, message in enumerate(messages)
    )


def _describe(name: str | None, description: str | None) -> str:
    return "".join(f"{line}\n" for line in (name, description) if line)


def transcript_to_prompt_text(entity: Entity) -> str:
    """Write a ``Transcript`` as text for the judge model, with labels to cite it by.

    Each message block is labelled ``B0``, ``B1`` and so on. Each line of a block is labelled
    ``L0``, ``L1`` and so on within that block. ``message_lines`` says what counts as a line.
    Empty lines are kept, without a label.

    ``TRANSCRIPT_LINES_FORMAT_INSTRUCTIONS_TEXT`` explains this text to the model.
    ``agent_run_line_citation_validator`` checks the citations into it.
    """
    if not isinstance(entity, Transcript):
        raise TypeError(
            f"transcript_to_prompt_text takes a Transcript, not {type(entity).__name__}"
        )
    return (
        _describe(entity.name, entity.description)
        + f"<|blocks|>\n{_blocks_to_prompt_text(entity.messages, '')}\n</|blocks|>"
    )


def agent_run_to_prompt_text(entity: Entity) -> str:
    """Write an ``AgentRun`` as text for the judge model, with labels to cite it by.

    The transcripts are written in the order of ``agent_run_transcripts``. Each message block is
    labelled ``T<n>B<n>``: the transcript's number, then the block's. Each line of a block is
    labelled ``L0``, ``L1`` and so on within that block. ``message_lines`` says what counts as a
    line.

    ``AGENT_RUN_LINES_FORMAT_INSTRUCTIONS_TEXT`` explains this text to the model.
    ``agent_run_line_citation_validator`` checks the citations into it.
    """
    if not isinstance(entity, AgentRun):
        raise TypeError(f"agent_run_to_prompt_text takes an AgentRun, not {type(entity).__name__}")
    transcripts = "\n".join(
        f"<|transcript T{index}|>\n"
        f"{_describe(transcript.name, transcript.description)}"
        f"<|T{index} blocks|>\n"
        f"{_blocks_to_prompt_text(transcript.messages, f'T{index}')}\n"
        f"</|T{index} blocks|>\n"
        f"</|transcript T{index}|>"
        for index, transcript in enumerate(agent_run_transcripts(entity))
    )
    return _describe(entity.name, entity.description) + transcripts


# Labels are written with the digits 0-9 and no leading zeros, and an address must be a label
# exactly as written.
_NUMBER = r"(0|[1-9][0-9]*)"
_AGENT_RUN_ADDRESS = re.compile(rf"T{_NUMBER}B{_NUMBER}")
_TRANSCRIPT_ADDRESS = re.compile(rf"B{_NUMBER}")


def _number(digits: str) -> int | None:
    """``digits`` as a number, or None if Python refuses to read a number that long."""
    try:
        return int(digits)
    except ValueError:
        return None


def _block_address_problem(entity: Entity, address: str) -> str | None:
    """What is wrong with ``address`` as the label of a block of ``entity``, or None if nothing is.

    ``address`` is the part of a line address that comes before its lines, such as ``T0B1``.
    """
    if isinstance(entity, AgentRun):
        match = _AGENT_RUN_ADDRESS.fullmatch(address)
        if match is None:
            return _AGENT_RUN_LINES_REQUIRED
        transcripts = agent_run_transcripts(entity)
        transcript_index, block_index = _number(match.group(1)), _number(match.group(2))
        if transcript_index is None or transcript_index >= len(transcripts):
            return f"the agent run has {len(transcripts)} transcript(s), numbered from T0"
        block_count = len(transcripts[transcript_index].messages)
        if block_index is None or block_index >= block_count:
            return f"T{transcript_index} has {block_count} block(s), numbered from B0"
    elif isinstance(entity, Transcript):
        match = _TRANSCRIPT_ADDRESS.fullmatch(address)
        if match is None:
            return _TRANSCRIPT_LINES_REQUIRED
        block_index = _number(match.group(1))
        if block_index is None or block_index >= len(entity.messages):
            return f"the transcript has {len(entity.messages)} block(s), numbered from B0"
    return None


_LINES = rf"L{_NUMBER}(?:-L?{_NUMBER})?"
_AGENT_RUN_LINE_ADDRESS = re.compile(rf"T{_NUMBER}B{_NUMBER}{_LINES}")
_TRANSCRIPT_LINE_ADDRESS = re.compile(rf"B{_NUMBER}{_LINES}")
# The shape of a block label or a line address, of an agent run or of a transcript.
_ANY_LABEL = rf"(?:T{_NUMBER})?B{_NUMBER}(?:{_LINES})?"
# A block or line label in brackets with no entity name, such as ``[T0B1L0-2]``.
_LINE_LABEL_WITHOUT_NAME = re.compile(rf"\[{_ANY_LABEL}\]")
_AGENT_RUN_LINES_REQUIRED = (
    "an agent run is cited by line, such as T0B1L0, or by a run of lines, such as T0B1L0-2"
)
_TRANSCRIPT_LINES_REQUIRED = (
    "a transcript is cited by line, such as B1L0, or by a run of lines, such as B1L0-2"
)
_ONLY_LINES_CAN_BE_CITED = (
    "only an agent run or a transcript can be cited here, by line or by a run of lines"
)


def _lines_problem(
    messages: Sequence[ChatMessage], block_label: str, match: re.Match[str]
) -> str | None:
    """What is wrong with the lines that a matched address names, in one transcript's ``messages``.

    The last three groups of ``match`` are the block, the first line, and the last line if there
    is one.
    """
    block_digits, first_digits, last_digits = match.groups()[-3:]
    block_index, first = _number(block_digits), _number(first_digits)
    last = first if last_digits is None else _number(last_digits)
    if block_index is None or block_index >= len(messages):
        return None
    line_count = len(message_lines(messages[block_index]))
    if first is None or last is None or last >= line_count:
        return f"{block_label} has {line_count} line(s), numbered from L0"
    if first > last:
        return (
            f"a run of lines is written from its first line to its last, such as {block_label}L0-2"
        )
    return None


def _line_address_problem(entity: Entity, address: str) -> str | None:
    """What is wrong with ``address`` as lines of ``entity``, or None if nothing is."""
    if isinstance(entity, AgentRun):
        match = _AGENT_RUN_LINE_ADDRESS.fullmatch(address)
        if match is None:
            return _AGENT_RUN_LINES_REQUIRED
        block_label = address[: address.index("L")]
        block_problem = _block_address_problem(entity, block_label)
        if block_problem is not None:
            return block_problem
        transcript = agent_run_transcripts(entity)[int(match.group(1))]
        return _lines_problem(transcript.messages, block_label, match)
    if isinstance(entity, Transcript):
        match = _TRANSCRIPT_LINE_ADDRESS.fullmatch(address)
        if match is None:
            return _TRANSCRIPT_LINES_REQUIRED
        block_label = address[: address.index("L")]
        block_problem = _block_address_problem(entity, block_label)
        if block_problem is not None:
            return block_problem
        return _lines_problem(entity.messages, block_label, match)
    # An entity of any other class has no lines, so nothing in it can be cited.
    return _ONLY_LINES_CAN_BE_CITED


def agent_run_line_citation_validator(
    entities: Mapping[str, Entity], output_field_value: JSONValue
) -> str | None:
    """Check the citations in one field of a judge's answer. Each one has to cite lines of an
    agent run or a transcript.

    Accepted:

    - ``[run:T0B1L0]``: one line of an agent run.
    - ``[run:T0B1L0-2]``: a run of lines.
    - ``[transcript:B1L0]``: a line of a transcript given by itself.

    Sent back to the model:

    - A line, a block or a transcript that does not exist.
    - A whole block (``[run:T0B1]``) or a whole entity (``[run]``).
    - Any citation of another kind of entity, such as a rubric: ``[rubric]`` or
      ``[rubric:paragraph 3]``. Only agent runs and transcripts have lines.
    - A label with no entity name (``[T0B1L0]``).
    - A citation that is not well formed: nothing after the colon (``[run:]``), a space before
      the colon (``[run :T0B1L0]``), or a space at the start or end of the address
      (``[run: T0B1L0]``).
    - A citation whose name is not an entity's: ``[agent_run:T0B1L0]``, or ``[Run]`` when the
      entity is named ``run``.

    Bracketed text that is not a citation is left alone, such as ``[sic]`` and
    ``[INFO: tests started]``.

    Args:
        entities: The entities given to ``judge``, under the names they were given.
        output_field_value: The value of a field of ``AGENT_RUN_LINE_CITATION_FORMAT``.

    Returns:
        None when every citation is accepted. Otherwise a message that lists each citation that
        is not, with the reason.
    """
    problems: list[str] = []
    required = {
        name: _AGENT_RUN_LINES_REQUIRED
        if isinstance(entity, AgentRun)
        else _TRANSCRIPT_LINES_REQUIRED
        if isinstance(entity, Transcript)
        else _ONLY_LINES_CAN_BE_CITED
        for name, entity in entities.items()
    }
    # A citation with no address cites the whole entity, which ``check_citation_addresses`` does
    # not look at, so it is found here.
    whole = re.compile(
        rf"\[({'|'.join(re.escape(name) for name in required)})\]" if required else r"(?!)"
    )
    for _, text in strings_in(output_field_value):
        problems.extend(misnamed_citation_problems(text, entities.keys(), _ANY_LABEL))
        problems.extend(malformed_citation_problems(text, entities.keys()))
        problems.extend(
            f"{match.group(0)} is not a citation: it has no entity name. Write the name, a colon"
            f" and the address, such as [<name>:{match.group(0)[1:-1]}]."
            for match in _LINE_LABEL_WITHOUT_NAME.finditer(text)
        )
        problems.extend(
            f"{match.group(0)} is not a citation of lines: {required[match.group(1)]}."
            for match in whole.finditer(text)
        )
    # An address with a space at its start or end was reported above as not well formed.
    address_problems = check_citation_addresses(
        entities,
        output_field_value,
        lambda entity, address: (
            None if is_padded_address(address) else _line_address_problem(entity, address)
        ),
    )
    if address_problems is not None:
        problems.append(address_problems)
    return "\n".join(problems) or None


AGENT_RUN_LINE_CITATION_FORMAT = CustomFormat(
    name="citations",
    value_schema={"type": "string"},
    validator=agent_run_line_citation_validator,
    entity_to_prompt_text={
        AgentRun: agent_run_to_prompt_text,
        Transcript: transcript_to_prompt_text,
    },
    entity_instructions={
        AgentRun: AGENT_RUN_LINES_FORMAT_INSTRUCTIONS_TEXT,
        Transcript: TRANSCRIPT_LINES_FORMAT_INSTRUCTIONS_TEXT,
    },
)
"""The custom format for a text field that cites agent runs and transcripts by line, as in
``[run:T0B3L4]``.

It holds three things:

- The functions that write an agent run and a transcript as prompt text.
- The text that explains to the model what those functions write.
- The validator, which requires every citation to name lines that exist.

``AGENT_RUN_LINE_CITATION_FORMAT.schema(description=...)`` is the schema of such a field. The
description is also a place to explain the addresses the evidence can be cited by.
"""
