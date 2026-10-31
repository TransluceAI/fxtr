"""Storage and JSON checkpoint contracts for behaviors records."""

import dataclasses
import json
from typing import Any

import pytest

from behaviors.implementations.context_policies.no_tools import NoTools
from behaviors.implementations.context_policies.scripted import ScriptedUserSimulator
from behaviors.implementations.context_policies.user_and_tools import (
    serializable_context_policy_from_user_and_tools,
)
from behaviors.implementations.models.from_api import chat_session_model_from_api
from behaviors.types import (
    AssistantMessage,
    CacheableContentText,
    CacheableToolParam,
    CacheControl,
    ContentReasoning,
    ContentRedactedReasoning,
    ContentSummarizedReasoning,
    ContentText,
    ContentToolUse,
    ContextTurn,
    DynamicData,
    JSONObject,
    JSONValue,
    ModelRequest,
    ModelResponse,
    ModelTurn,
    RetryConfig,
    SamplingParams,
    SessionInitialization,
    SetSystemMessage,
    StopEndTurn,
    SystemMessage,
    ToolMessage,
    ToolParam,
    Transcript,
    TranscriptGroup,
    Usage,
    UserMessage,
    UserTurn,
    find_metadata,
)
from fxtr.core import json as fxtr_json
from fxtr.core.entity_data import thaw_fxtr_data
from fxtr.entity_defns.dataclass_bases import ConversionError
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.storage.memory_storage import MemoryStorage


def _json_roundtrip(value: DynamicData) -> DynamicData:
    """Write a tagged value as plain JSON and read it back."""
    data = thaw_fxtr_data(value.data)
    assert isinstance(data, dict)
    return DynamicData.from_data(json.loads(json.dumps(data)))


def test_json_aliases_are_shared() -> None:
    assert JSONObject is fxtr_json.JSONObject
    assert JSONValue is fxtr_json.JSONValue


@pytest.mark.anyio
async def test_initialization_load_names_the_concrete_class() -> None:
    store = MemoryStorage()
    original = SetSystemMessage(SystemMessage((CacheableContentText("instructions"),)))
    ref = await store.store(original)
    assert await store.load(ref) == original
    assert await store.load(ref.entity_id, as_type=SetSystemMessage) == original
    with pytest.raises(TypeError):
        await store.load(ref.entity_id, as_type=SessionInitialization)


@pytest.mark.anyio
async def test_group_stores_its_full_nonempty_tree() -> None:
    transcript = Transcript("attempt 1", None, (UserMessage((ContentText("question"),)),))
    leaf = TranscriptGroup("attempts", None, (transcript,), (), {"scores": [0, 0.5, True, None]})
    inner = TranscriptGroup("task", None, (), (leaf,))
    original = TranscriptGroup("run", None, (transcript,), (inner,))
    store = MemoryStorage()
    ref = await store.store(original)
    restored = await store.load(ref)
    assert restored == original
    assert restored.groups[0].groups[0].transcripts[0] == transcript
    assert (await store.store(restored)).entity_id == ref.entity_id
    raw = await store.load_raw_data(ref.entity_id)
    assert isinstance(raw, dict)
    assert isinstance(raw["groups"], list)
    assert isinstance(raw["groups"][0], dict)


@dataclasses.dataclass(frozen=True)
class StoredConfiguration(
    DataclassEntity, fxtr_type="org.transluce.behaviors.tests.Configuration.v1"
):
    initialization: SetSystemMessage | None
    sampling: SamplingParams
    retry: RetryConfig


@pytest.mark.anyio
@pytest.mark.parametrize("with_initialization", [False, True])
async def test_embedded_configuration_roundtrip(with_initialization: bool) -> None:
    original = StoredConfiguration(
        initialization=SetSystemMessage(SystemMessage((ContentText("system"),)))
        if with_initialization
        else None,
        sampling=SamplingParams(temperature=0.0, extra={"scores": [1, 1.0, True, False, None]}),
        retry=RetryConfig(initial_backoff_seconds=0.0, jitter_seconds=0.0),
    )
    store = MemoryStorage()
    ref = await store.store(original)
    restored = await store.load(ref)
    assert restored == original
    scores = restored.sampling.extra["scores"]
    assert isinstance(scores, list)
    assert [type(score) for score in scores] == [int, float, bool, bool, type(None)]
    assert (await store.store(restored)).entity_id == ref.entity_id


@pytest.mark.anyio
@pytest.mark.parametrize("value", [-(2**64), 2**64 - 1, 0, 0.0, True, False, None])
async def test_metadata_preserves_supported_number_boundaries(value: JSONValue) -> None:
    original = Transcript("measurement", None, (), {"measurement": {"scores": [value]}})
    store = MemoryStorage()
    restored = await store.load(await store.store(original))
    measurement = restored.metadata["measurement"]
    assert isinstance(measurement, dict)
    scores = measurement["scores"]
    assert isinstance(scores, list)
    assert scores == [value]
    assert type(scores[0]) is type(value)


@pytest.mark.anyio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 2**64, -(2**64) - 1])
@pytest.mark.parametrize("location", ["metadata", "tool_input"])
async def test_stored_json_obeys_codec_number_limits(value: float | int, location: str) -> None:
    payload: JSONObject = {"measurements": [{"score": value}]}
    original = Transcript(
        "measurement",
        None,
        (AssistantMessage((ContentToolUse("call", "measure", payload, None),)),)
        if location == "tool_input"
        else (),
        payload if location == "metadata" else {},
    )
    with pytest.raises(ValueError):
        await MemoryStorage().store(original)


@pytest.mark.anyio
@pytest.mark.parametrize("invalid", [b"bytes", {1: "non-string key"}, (1, 2)])
async def test_stored_metadata_rejects_non_json_values(invalid: Any) -> None:
    original = Transcript("measurement", None, (), {"payload": invalid})
    with pytest.raises(ConversionError):
        await MemoryStorage().store(original)


@pytest.mark.anyio
async def test_stored_metadata_rejects_entity_references() -> None:
    store = MemoryStorage()
    ref = await store.store(Transcript("source", None, ()))
    invalid: Any = ref.entity_id
    with pytest.raises(ConversionError):
        await store.store(Transcript("measurement", None, (), {"payload": invalid}))


def _all_messages() -> tuple[SystemMessage, UserMessage, AssistantMessage, ToolMessage]:
    texts = (
        ContentText("plain"),
        CacheableContentText("cacheable without hint"),
        CacheableContentText("cached", cache_control=CacheControl(ttl="5m")),
    )
    return (
        SystemMessage(texts),
        UserMessage(texts),
        AssistantMessage(
            texts
            + (
                ContentReasoning("thinking"),
                ContentSummarizedReasoning("summary", "signature"),
                ContentRedactedReasoning("encrypted"),
                ContentToolUse("call", "measure", {"scores": [1, 1.0, True, None]}, None),
            )
        ),
        ToolMessage(texts, "call", False),
    )


@pytest.mark.anyio
async def test_all_message_roles_preserve_cacheable_identity_in_storage() -> None:
    original = Transcript("conversation", None, _all_messages())
    store = MemoryStorage()
    restored = await store.load(await store.store(original))
    assert restored == original
    for message in restored.messages:
        assert type(message.content[0]) is ContentText
        assert type(message.content[1]) is CacheableContentText
        assert type(message.content[2]) is CacheableContentText


def test_json_roundtrip_preserves_tool_variants_and_json() -> None:
    original = ModelRequest(
        context=_all_messages(),
        tools=(
            ToolParam("plain", None, {}, None, None),
            CacheableToolParam("without_hint", None, {}, None, None),
            CacheableToolParam(
                "cached", None, {"nested": [1, 1.0, True, {}]}, None, ({},), CacheControl()
            ),
        ),
        tool_choice=None,
        parallel_tool_calls=False,
        sampling=SamplingParams(extra={"nested": {"scores": [1, 1.0, True, None], "empty": []}}),
    )
    restored = _json_roundtrip(DynamicData.from_tagged(original)).as_type(ModelRequest)
    assert restored == original
    assert [type(tool) for tool in restored.tools] == [
        ToolParam,
        CacheableToolParam,
        CacheableToolParam,
    ]
    nested = restored.sampling.extra["nested"]
    assert isinstance(nested, dict)
    scores = nested["scores"]
    assert isinstance(scores, list)
    assert [type(score) for score in scores] == [int, float, bool, type(None)]


class _CapturingAPI:
    def __init__(self, response: ModelResponse):
        self.response = response
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self.response


@pytest.mark.anyio
async def test_api_checkpoint_and_turn_metadata_preserve_content_variants() -> None:
    system, user, assistant, tool = _all_messages()
    response = ModelResponse("response", (assistant,), StopEndTurn(), Usage(3, None, None, 2, None))
    api = _CapturingAPI(response)
    model = chat_session_model_from_api(api, SamplingParams())
    session = await model.start_session(SetSystemMessage(system))
    turn = await session.generate_turn(ContextTurn((user, tool), None))
    assert isinstance(turn, ModelTurn)
    persisted_metadata = tuple(_json_roundtrip(entry) for entry in turn.metadata)
    assert find_metadata(persisted_metadata, ModelResponse) == response
    state = _json_roundtrip(await session.get_state())
    restored = await model.restore_session(state)
    assert await restored.get_state() == state
    await restored.generate_turn(ContextTurn((), None))
    assert api.requests[-1].context == (system, user, tool, assistant)
    for message in api.requests[-1].context:
        assert type(message.content[1]) is CacheableContentText
    await session.aclose()
    await restored.aclose()


@pytest.mark.anyio
async def test_scripted_checkpoint_preserves_cacheable_text_without_hint() -> None:
    message = UserMessage((CacheableContentText("instructions"),))
    simulator = ScriptedUserSimulator((message,))
    session = await simulator.start_session()
    state = _json_roundtrip(await session.get_state())
    restored = await simulator.restore_session(state)
    turn = await restored.next_user_turn(None)
    assert isinstance(turn, UserTurn)
    assert turn.message == message
    assert type(turn.message.content[0]) is CacheableContentText
    await session.aclose()
    await restored.aclose()


@pytest.mark.anyio
async def test_combined_checkpoint_preserves_pending_cacheable_variants() -> None:
    _, user, assistant, _ = _all_messages()
    policy = serializable_context_policy_from_user_and_tools(
        ScriptedUserSimulator((user, user)),
        NoTools(),
        initialization=None,
        show_tool_calls_to_user=True,
    )
    session = await policy.start_session()
    await session.generate_context_turn(None)
    await session.generate_context_turn(ModelTurn((assistant,)))
    state = _json_roundtrip(await session.get_state())
    restored = await policy.restore_session(state)
    assert await restored.get_state() == state
    completed = ModelTurn((AssistantMessage((ContentText("done"),)),))
    assert await session.generate_context_turn(completed) == await restored.generate_context_turn(
        completed
    )
    await session.aclose()
    await restored.aclose()


@pytest.mark.anyio
async def test_checkpoint_with_unknown_content_tag_is_rejected() -> None:
    simulator = ScriptedUserSimulator((UserMessage((ContentText("hello"),)),))
    session = await simulator.start_session()
    state: Any = thaw_fxtr_data((await session.get_state()).data)
    state["messages"][0]["content"][0]["$type"] = "org.example.Unsupported.v1"
    with pytest.raises(ConversionError):
        await simulator.restore_session(DynamicData.from_data(state))
    await session.aclose()
