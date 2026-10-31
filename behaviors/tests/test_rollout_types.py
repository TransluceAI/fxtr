"""Tests for rollout, transcript, and response types."""

import dataclasses
import re
from pathlib import Path

import pytest

from behaviors.types import (
    AgentRun,
    AssistantMessage,
    ChatRollout,
    ContentText,
    ContextTurn,
    EndRollout,
    JSONObject,
    ModelCallFailure,
    ModelRequest,
    ModelResponse,
    ModelTurn,
    RolloutCompleted,
    RolloutFailed,
    RolloutMaxTurns,
    RolloutStatus,
    SamplingParams,
    SessionInitialization,
    SetSystemMessage,
    StopEndTurn,
    StopReason,
    StopStopSequence,
    SystemMessage,
    ToolChoiceTool,
    ToolParam,
    Transcript,
    TranscriptGroup,
    Usage,
    UserMessage,
    find_metadata,
)
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dataclass_bases import ConversionError, TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData
from fxtr.storage.memory_storage import MemoryStorage


def _context_turn() -> ContextTurn:
    return ContextTurn(
        messages=(
            SystemMessage(content=(ContentText(text="be helpful"),)),
            UserMessage(content=(ContentText(text="hi"),)),
        ),
        available_tools=(
            ToolParam(
                name="search",
                description=None,
                input_schema={"type": "object"},
                strict=None,
                input_examples=None,
            ),
        ),
    )


def _model_turn() -> ModelTurn:
    return ModelTurn(
        messages=(AssistantMessage(content=(ContentText(text="hello!"),)),),
    )


def test_rollout_construct() -> None:
    rollout = ChatRollout(
        context_turns=(_context_turn(),),
        model_turns=(_model_turn(),),
        status=RolloutCompleted(),
        end=EndRollout(),
    )
    assert len(rollout.context_turns) == len(rollout.model_turns) == 1
    assert rollout.status == RolloutCompleted()


@dataclasses.dataclass(frozen=True)
class PilotAlternatives(TaggedDataclass, fxtr_type="test.behaviors.PilotAlternatives.v1"):
    alternatives: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class InfoBox(TaggedDataclass, fxtr_type="test.behaviors.InfoBox.v1"):
    actual_model: str
    content: str | None


@dataclasses.dataclass(frozen=True)
class FinalView(TaggedDataclass, fxtr_type="test.behaviors.FinalView.v1"):
    view: float
    per_sample_views: tuple[float, ...]


@dataclasses.dataclass(frozen=True)
class Seed(TaggedDataclass, fxtr_type="test.behaviors.Seed.v1"):
    value: int


def _meta(*values: TaggedDataclass) -> tuple[DynamicData, ...]:
    return tuple(DynamicData.from_tagged(value) for value in values)


@dataclasses.dataclass(frozen=True)
class CustomInitialization(SessionInitialization, fxtr_type="test.behaviors.CustomInitialization"):
    environment: str


@pytest.mark.anyio
@pytest.mark.parametrize(
    "initialization",
    [
        None,
        SetSystemMessage(SystemMessage((ContentText("exact system prompt"),))),
        CustomInitialization(environment="custom environment"),
    ],
)
async def test_rollout_initialization_roundtrip(
    initialization: SessionInitialization | None,
) -> None:
    recorded = None if initialization is None else DynamicData.from_tagged(initialization)
    rollout = ChatRollout((), (), RolloutCompleted(), EndRollout(), initialization=recorded)
    store = MemoryStorage()
    reference = await store.store(rollout)
    restored = await store.load(reference)
    assert restored == rollout
    if initialization is not None:
        assert restored.initialization is not None
        assert restored.initialization.as_type(type(initialization)) == initialization
    assert (await store.store(restored)).entity_id == reference.entity_id


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["initialization", "metadata"])
async def test_rollout_requires_defaulted_fields_in_stored_data(field: str) -> None:
    store = MemoryStorage()
    value = ChatRollout((), (), RolloutCompleted(), EndRollout())
    raw = await store.load_raw_data((await store.store(value)).entity_id)
    assert isinstance(raw, dict)
    raw.pop(field)
    with pytest.raises(ConversionError):
        await store.load(await store.store_raw_data(raw), as_type=ChatRollout)


def test_viewer_type_tags_match_python() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "ts/packages/fxtr-view-behaviors/src/types.ts"
    ).read_text()
    tags = dict(re.findall(r'^export const (\w+) = "([^"]+)";', source, re.MULTILINE))
    types = {
        "CHAT_ROLLOUT": ChatRollout,
        "CONTEXT_TURN": ContextTurn,
        "MODEL_TURN": ModelTurn,
        "END_ROLLOUT": EndRollout,
        "TRANSCRIPT": Transcript,
        "TRANSCRIPT_GROUP": TranscriptGroup,
        "AGENT_RUN": AgentRun,
        "SET_SYSTEM_MESSAGE": SetSystemMessage,
        "MODEL_RESPONSE": ModelResponse,
    }
    assert tags == {name: cls.fxtr_type_id() for name, cls in types.items()}


def test_rollout_status_union() -> None:
    statuses: tuple[RolloutStatus, ...] = (
        RolloutCompleted(),
        RolloutMaxTurns(max_turns=3),
    )
    truncated = [s for s in statuses if isinstance(s, RolloutMaxTurns)]
    assert [t.max_turns for t in truncated] == [3]


@pytest.mark.anyio
async def test_rollout_serialization_roundtrip() -> None:
    rollout = ChatRollout(
        context_turns=(_context_turn(), _context_turn()),
        model_turns=(_model_turn(),),
        status=RolloutFailed(
            failure=ModelCallFailure(
                category="context_length",
                description="prompt is too long",
                provider_http_status=400,
            )
        ),
        end=None,
    )
    store = MemoryStorage()
    ref = await store.store(rollout)
    restored = await store.load(ref)
    assert (await store.store(restored)).entity_id == ref.entity_id
    assert restored == rollout
    assert isinstance(restored.status, RolloutFailed)
    assert restored.status.failure.category == "context_length"


@pytest.mark.anyio
async def test_nested_json_serialization_roundtrip() -> None:
    schema: JSONObject = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
        "additionalProperties": False,
        "examples": [{"query": "cats", "limit": 5}, {"query": "dogs"}],
        "default": None,
        "threshold": 0.5,
    }
    rollout = ChatRollout(
        context_turns=(
            ContextTurn(
                messages=(UserMessage(content=(ContentText(text="hi"),)),),
                available_tools=(
                    ToolParam(
                        name="search",
                        description=None,
                        input_schema=schema,
                        strict=None,
                        input_examples=None,
                    ),
                ),
            ),
        ),
        model_turns=(_model_turn(),),
        status=RolloutCompleted(),
        end=EndRollout(),
    )
    store = MemoryStorage()
    restored = await store.load(await store.store(rollout))
    assert restored == rollout

    tools = restored.context_turns[0].available_tools
    assert tools is not None
    restored_schema = tools[0].input_schema
    assert restored_schema == schema
    assert restored_schema["additionalProperties"] is False
    assert restored_schema["threshold"] == 0.5
    assert restored_schema["default"] is None


@pytest.mark.anyio
async def test_rollout_metadata_roundtrip() -> None:
    base = _context_turn()
    rollout = ChatRollout(
        context_turns=(
            ContextTurn(
                messages=base.messages,
                available_tools=base.available_tools,
                metadata=_meta(PilotAlternatives(("a", "b"))),
            ),
        ),
        model_turns=(
            ModelTurn(
                messages=_model_turn().messages,
                metadata=_meta(InfoBox("gpt-x", None), Seed(3)),
            ),
        ),
        status=RolloutCompleted(),
        end=EndRollout(metadata=_meta(FinalView(0.25, (0.2, 0.3)))),
        metadata=_meta(Seed(7)),
    )
    store = MemoryStorage()
    reference = await store.store(rollout)
    restored = await store.load(reference)
    assert restored == rollout
    assert (await store.store(restored)).entity_id == reference.entity_id
    assert restored.end is not None
    assert find_metadata(restored.end.metadata, FinalView) == FinalView(0.25, (0.2, 0.3))
    assert find_metadata(restored.context_turns[0].metadata, PilotAlternatives) == (
        PilotAlternatives(("a", "b"))
    )
    model_metadata = restored.model_turns[0].metadata
    assert find_metadata(model_metadata, InfoBox) == InfoBox("gpt-x", None)
    assert find_metadata(model_metadata, Seed) == Seed(3)
    assert find_metadata(model_metadata, FinalView) is None
    assert find_metadata(restored.metadata, Seed) == Seed(7)


@pytest.mark.anyio
async def test_metadata_wire_form_matches_concrete_value() -> None:
    store = MemoryStorage()
    rollout = ChatRollout((), (), RolloutCompleted(), EndRollout(), metadata=_meta(Seed(7)))
    raw = await store.load_raw_data((await store.store(rollout)).entity_id)
    assert isinstance(raw, dict)
    assert raw["metadata"] == [DEFAULT_CONVERTER.unstructure_entity(Seed(7))]


@pytest.mark.anyio
async def test_session_initialization_roundtrip() -> None:
    initialization = SetSystemMessage(
        message=SystemMessage(content=(ContentText(text="be helpful"),))
    )
    store = MemoryStorage()
    restored = await store.load(await store.store(initialization))
    assert restored == initialization


def test_transcript_and_agent_run() -> None:
    transcript = Transcript(
        name="rollout",
        description=None,
        messages=(UserMessage(content=(ContentText(text="say hello"),)),),
        metadata={"subject_model": "gpt-5.4"},
    )
    child_group = TranscriptGroup(
        name="sessions",
        description=None,
        transcripts=(Transcript(name="assistant", description=None, messages=transcript.messages),),
        groups=(),
    )
    run = AgentRun(
        name="run_1",
        description=None,
        transcripts=(transcript,),
        groups=(child_group,),
    )
    assert run.transcripts[0].metadata["subject_model"] == "gpt-5.4"
    assert run.groups[0].transcripts[0].name == "assistant"


@pytest.mark.anyio
async def test_agent_run_store_load_roundtrip() -> None:
    transcript = Transcript(
        name="rollout",
        description=None,
        messages=(UserMessage(content=(ContentText(text="say hello"),)),),
    )
    run = AgentRun(
        name="run_1",
        description=None,
        transcripts=(transcript,),
        groups=(
            TranscriptGroup(
                name="sessions",
                description=None,
                transcripts=(transcript,),
                groups=(
                    TranscriptGroup(
                        name="nested",
                        description=None,
                        transcripts=(transcript,),
                        groups=(),
                        metadata={"depth": 2},
                    ),
                ),
            ),
        ),
    )
    store = MemoryStorage()
    ref = await store.store(run)
    loaded = await store.load(ref)
    assert loaded == run
    assert loaded.groups[0].transcripts[0].name == "rollout"
    assert loaded.groups[0].groups[0].metadata == {"depth": 2}
    assert (await store.store(loaded)).entity_id == ref.entity_id


@pytest.mark.anyio
@pytest.mark.parametrize("status", [RolloutCompleted(), RolloutMaxTurns(max_turns=1)])
async def test_rollout_status_storage_roundtrip(status: RolloutStatus) -> None:
    rollout = ChatRollout(
        context_turns=(_context_turn(),),
        model_turns=(_model_turn(),),
        status=status,
        end=EndRollout(metadata=_meta(Seed(1))) if isinstance(status, RolloutCompleted) else None,
    )
    store = MemoryStorage()
    ref = await store.store(rollout)
    assert await store.load(ref) == rollout
    assert ref.entity_id == rollout.content_id()


def test_response_info() -> None:
    response = ModelResponse(
        id="msg_1",
        output=(AssistantMessage(content=(ContentText(text="4"),)),),
        stop_reason=StopEndTurn(),
        usage=Usage(
            input_tokens=10,
            cache_read_input_tokens=100,
            cache_write_input_tokens=0,
            output_tokens=1,
            reasoning_output_tokens=None,
        ),
    )
    assert isinstance(response.stop_reason, StopEndTurn)

    stop: StopReason = StopStopSequence(stop_sequence="</answer>")
    assert isinstance(stop, StopStopSequence) and stop.stop_sequence == "</answer>"


def test_request_params() -> None:
    request = ModelRequest(
        context=(UserMessage(content=(ContentText(text="hi"),)),),
        tools=(),
        tool_choice=None,
        parallel_tool_calls=None,
        sampling=SamplingParams(max_tokens=1024, stop_sequences=("</answer>",)),
    )
    assert request.sampling.max_tokens == 1024

    forced = ModelRequest(
        context=(UserMessage(content=(ContentText(text="hi"),)),),
        tools=(),
        tool_choice=ToolChoiceTool(name="get_weather"),
        parallel_tool_calls=False,
        sampling=SamplingParams(),
    )
    assert isinstance(forced.tool_choice, ToolChoiceTool)
    assert forced.tool_choice.name == "get_weather"
