"""The OpenAI experiment end to end, with fake models: conversations, transcripts, judgments,
verdicts and the report, crossing the fxtr step, workflow and storage boundaries."""

import json
import re
from dataclasses import replace

import pytest

from behavior_rollouts import instructions_in_force, transcript_judging
from behavior_rollouts.echo import EchoAPI
from behavior_rollouts.instructions_in_force import (
    OUTPUT_SCHEMA,
    RUBRIC,
    InstructionsInForceReport,
)
from behavior_rollouts.instructions_in_force_sequences import INSTRUCTION_SEQUENCES
from behavior_rollouts.models import store_model_specs
from behavior_rollouts.transcript_judging import JUDGE_INSTRUCTIONS, TRANSCRIPT
from behaviors.judging.judge import Judgment
from behaviors.types import (
    AssistantMessage,
    ChatRollout,
    ContentText,
    ModelRequest,
    ModelResponse,
    ReasoningParams,
    RolloutCompleted,
    SamplingParams,
    SystemMessage,
    TextEntity,
    Transcript,
    find_metadata,
)
from fxtr.core.entities import BoundID
from fxtr.entity_defns.array import Array, ArrayType
from fxtr.project.client import ProjectClient
from fxtr.project.running import run_job

pytestmark = pytest.mark.anyio

_BLOCK = re.compile(r"<\|B(\d+); role: (\w+)\|>")
_LINE = re.compile(r"<\|L(\d+)\|>(.*)")


def transcript_lines(text: str) -> list[tuple[str, str, str]]:
    """The (role, address, text) of each labelled line of a transcript as the judge reads it."""
    lines: list[tuple[str, str, str]] = []
    block, role = "", ""
    for line in text.splitlines():
        if found := _BLOCK.fullmatch(line):
            block, role = found.group(1), found.group(2)
        elif found := _LINE.match(line):
            lines.append((role, f"B{block}L{found.group(1)}", found.group(2)))
    return lines


class FakeModel(EchoAPI):
    """A subject that echoes each user turn under its name, or a judge that finds a violation
    where a reply says "part" (as the banned word "party" does), citing the transcript's lines.

    The judge named ``fussy-judge`` first cites a whole block, which ``judge`` sends back, and
    answers correctly when asked again."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.requests: list[ModelRequest] = []
        self.closed = False
        clients.append(self)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        response = await super().generate(request)
        if self.model_name.endswith("judge"):
            text = self.judge(request)
        else:
            echo = "".join(b.text for b in response.output[0].content if isinstance(b, ContentText))
            text = f"{self.model_name}: {echo}"
        return replace(response, output=(AssistantMessage(content=(ContentText(text=text),)),))

    def judge(self, request: ModelRequest) -> str:
        prompt = "".join(
            block.text for block in request.context[1].content if isinstance(block, ContentText)
        )
        lines = transcript_lines(prompt)
        instruction = next(address for role, address, _ in lines if role == "user")
        violation = next(
            (address for role, address, text in lines if role == "assistant" and "part" in text),
            None,
        )
        if self.model_name == "fussy-judge" and len(request.context) == 2:
            discussion = f"The whole reply breaks the rule [{TRANSCRIPT}:B2]."
        elif violation is None:
            discussion = f"Every reply keeps to the instruction [{TRANSCRIPT}:{instruction}]."
        else:
            discussion = (
                f"The user set a rule [{TRANSCRIPT}:{instruction}], which a reply breaks"
                f" [{TRANSCRIPT}:{violation}]."
            )
        first = "" if violation is None else f"[{TRANSCRIPT}:{violation}]"
        return json.dumps(
            {"discussion": discussion, "match": violation is not None, "first_violation": first}
        )

    async def aclose(self) -> None:
        self.closed = True


clients: list[FakeModel] = []


async def test_conversations_are_held_turned_into_transcripts_and_judged(
    monkeypatch: pytest.MonkeyPatch,
    project_client: ProjectClient,
) -> None:
    clients.clear()
    monkeypatch.setattr(instructions_in_force, "make_api", FakeModel)
    monkeypatch.setattr(transcript_judging, "make_api", FakeModel)
    models = ["test-small", "test-large"]
    judges = ["plain-judge", "fussy-judge:low"]
    model_name, sampling = await store_model_specs(
        project_client, models, dim="model", max_tokens=128
    )
    judge_model_name, judge_sampling = await store_model_specs(
        project_client, judges, dim="judge", max_tokens=256
    )
    samples = 2
    result = await run_job(
        project_client,
        instructions_in_force.experiment,
        inputs={
            "sequences": instructions_in_force.sequences_array(INSTRUCTION_SEQUENCES),
            "model_name": model_name,
            "sampling": sampling,
            "judge_model_name": judge_model_name,
            "judge_sampling": judge_sampling,
            "judge_instructions": Array.scalar(
                await project_client.store(JUDGE_INSTRUCTIONS), BoundID[TextEntity]
            ),
            "rubric": Array.scalar(await project_client.store(RUBRIC), BoundID[TextEntity]),
            "output_schema": Array.scalar(
                await project_client.store(TextEntity(text=json.dumps(OUTPUT_SCHEMA))),
                BoundID[TextEntity],
            ),
            "judge_samples": Array.scalar(samples, int),
        },
    )
    assert result.type == ArrayType.of(("[]", BoundID[InstructionsInForceReport]))
    report = await project_client.load(result.item(), as_type=InstructionsInForceReport)
    rollouts = await project_client.load(report.rollouts)
    transcripts = await project_client.load(report.transcripts)
    judgments = await project_client.load(report.judgments)
    verdicts = await project_client.load(report.verdicts)
    conversations = {(model, name) for model in models for name in INSTRUCTION_SEQUENCES}
    assert set(rollouts.keys()) == set(transcripts.keys()) == conversations
    judged = {
        (*key, judge, sample)
        for key in conversations
        for judge in judges
        for sample in range(samples)
    }
    assert set(judgments.keys()) == set(verdicts.keys()) == judged
    # A conversation and its transcript each, a judgment each, and the verdicts.
    entries = await project_client.list_cache_entries()
    assert len(entries) == 2 * len(conversations) + len(judged) + 1

    # Each subject client holds one conversation, sending its turns in order.
    subjects = [client for client in clients if not client.model_name.endswith("judge")]
    assert len(subjects) == len(conversations)
    for (model, name), reference in rollouts.items():
        assert isinstance(name, str)
        turns = INSTRUCTION_SEQUENCES[name]
        rollout = await project_client.load(reference, as_type=ChatRollout)
        assert isinstance(rollout.status, RolloutCompleted)
        assert rollout.end is not None
        assert find_metadata(rollout.end.metadata, instructions_in_force.TurnsSent) == (
            instructions_in_force.TurnsSent(turns_sent=len(turns))
        )
        assert [
            find_metadata(turn.metadata, instructions_in_force.TurnIndex)
            for turn in rollout.context_turns
        ] == [instructions_in_force.TurnIndex(turn_index=index) for index in range(len(turns))]
        assert [turn.messages[0].content for turn in rollout.model_turns] == [
            (ContentText(text=f"{model}: Echo: {turn}"),) for turn in turns
        ]
        # The transcript is the system message, then the turns' messages, in order.
        transcript = await project_client.load(transcripts[model, name], as_type=Transcript)
        assert isinstance(transcript.messages[0], SystemMessage)
        assert len(transcript.messages) == 1 + 2 * len(turns)

    # Every judgment refers to the stored transcript it judged, and cites it by line.
    instructions_id, rubric_id = JUDGE_INSTRUCTIONS.content_id(), RUBRIC.content_id()
    for (model, name, judge, sample), reference in judgments.items():
        judgment = await project_client.load(reference, as_type=Judgment)
        transcript_id = transcripts[model, name].entity_id
        assert dict(judgment.entity_ids) == {
            "instructions": instructions_id,
            "rubric": rubric_id,
            TRANSCRIPT: transcript_id,
        }
        assert judgment.output is not None
        expected = any("part" in turn for turn in INSTRUCTION_SEQUENCES[str(name)])
        assert judgment.output["match"] is expected
        assert judgment.citations
        for citation in judgment.citations:
            assert citation.entity_id == transcript_id
            assert citation.address_in_entity is not None
            assert re.fullmatch(r"B\d+L\d+", citation.address_in_entity)
        assert (
            any(c.path_to_output_field == ("first_violation",) for c in judgment.citations)
            is expected
        )
        # The fussy judge's first answer cited a whole block, so it was asked to correct it.
        fussy = judge == "fussy-judge:low"
        assert len(judgment.attempts) == (2 if fussy else 1)
        if fussy:
            assert judgment.attempts[0].invalid_output_reasons
        assert verdicts[model, name, judge, sample] == {
            "violation": expected,
            "answers": 2 if fussy else 1,
        }
        # Every request the judgment made used its judge's sampling.
        effort = None if judge == "plain-judge" else ReasoningParams(effort="low")
        judge_params = SamplingParams(max_tokens=256, reasoning=effort)
        assert all(attempt.request.sampling == judge_params for attempt in judgment.attempts)
    # Each judge was called through the API built for its model name, and each subject with its
    # own sampling.
    judge_clients = [client for client in clients if client.model_name.endswith("judge")]
    assert {client.model_name for client in judge_clients} == {"plain-judge", "fussy-judge"}
    reasoning = {r.sampling.reasoning for c in judge_clients for r in c.requests}
    assert reasoning == {None, ReasoningParams(effort="low")}
    assert all(r.sampling == SamplingParams(max_tokens=128) for c in subjects for r in c.requests)
    assert all(client.closed for client in clients)
    assert await instructions_in_force.report_results(project_client, result) == 0


@pytest.mark.parametrize("sequences", [{}, {"empty": []}, {"blank": [" "]}, {"": ["Hello"]}])
async def test_sequences_reject_empty_data(sequences: dict[str, list[str]]) -> None:
    with pytest.raises(ValueError, match="nonempty"):
        instructions_in_force.sequences_array(sequences)
