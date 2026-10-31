"""End-to-end test of the model/policy protocols composing through run_chat_rollout."""

import dataclasses
import functools
import json
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import pytest

from behaviors.implementations.context_policies import (
    GeneratorContextPolicy,
    NoTools,
    ScriptedUserSimulator,
    context_policy_from_generator,
    context_policy_from_tool_policy,
    context_policy_from_user_and_tools,
    context_policy_from_user_simulator,
    serializable_context_policy_from_user_and_tools,
)
from behaviors.implementations.models import APIBackedChatSession, chat_session_model_from_api
from behaviors.interfaces import (
    ModelAPI,
    ModelCallFailedError,
    ModelError,
    SerializableChatSessionModel,
    SerializableStreamingChatSessionModel,
    SerializableToolPolicy,
    SerializableToolPolicySession,
    SerializableUserSimulator,
    StreamingChatSession,
    ToolPolicy,
    UserSimulator,
    UserSimulatorSession,
)
from behaviors.rollouts.chat import (
    ChatRolloutEvent,
    ContextTurnEvent,
    ModelStreamEvent,
    ModelTurnEvent,
    run_chat_rollout,
)
from behaviors.types import (
    AssistantMessage,
    ChatMessage,
    ContentText,
    ContentToolUse,
    ContextTurn,
    DynamicData,
    EndRollout,
    ModelCallFailure,
    ModelRequest,
    ModelResponse,
    ModelTurn,
    RetryConfig,
    RolloutCompleted,
    RolloutFailed,
    RolloutMaxTurns,
    SamplingParams,
    SessionInitialization,
    SetSystemMessage,
    StopEndTurn,
    StreamEvent,
    StreamEventCallback,
    StreamRestart,
    StreamTextDelta,
    SystemMessage,
    ToolMessage,
    ToolParam,
    ToolTurn,
    Usage,
    UserMessage,
    UserTurn,
    find_metadata,
)
from fxtr.core.entity_data import thaw_fxtr_data
from fxtr.entity_defns.dataclass_bases import ConversionError, TaggedDataclass


@dataclasses.dataclass(frozen=True)
class Note(TaggedDataclass, fxtr_type="org.transluce.behaviors.tests.Note.v1"):
    text: str


@dataclasses.dataclass(frozen=True)
class Count(TaggedDataclass, fxtr_type="org.transluce.behaviors.tests.Count.v1"):
    value: int


def _meta(*values: TaggedDataclass) -> tuple[DynamicData, ...]:
    return tuple(DynamicData.from_tagged(value) for value in values)


def _state_data(state: DynamicData) -> dict[str, Any]:
    data = thaw_fxtr_data(state.data)
    assert isinstance(data, dict)
    return data


def _json_roundtrip(state: DynamicData) -> DynamicData:
    """Persist a state as plain JSON and read it back, as a caller checkpointing to disk would."""
    return DynamicData.from_data(json.loads(json.dumps(_state_data(state))))


def _user(text: str) -> UserMessage:
    return UserMessage(content=(ContentText(text=text),))


def _response(*content: ContentText | ContentToolUse) -> ModelResponse:
    return ModelResponse(
        id="msg_1",
        output=(AssistantMessage(content=content),),
        stop_reason=StopEndTurn(),
        usage=Usage(
            input_tokens=1,
            cache_read_input_tokens=None,
            cache_write_input_tokens=None,
            output_tokens=1,
            reasoning_output_tokens=None,
        ),
    )


class ScriptedModelAPI:
    """Returns the given responses/failures, one per call, in order."""

    def __init__(self, outputs: tuple[ModelResponse | ModelCallFailure, ...]):
        self._outputs = list(outputs)
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse | ModelCallFailure:
        self.requests.append(request)
        return self._outputs.pop(0)


class StreamingScriptedModelAPI(ScriptedModelAPI):
    """Scripted API that streams two text deltas before each successful response."""

    async def generate_with_events(
        self, request: ModelRequest, callback: StreamEventCallback
    ) -> ModelResponse | ModelCallFailure:
        result = await self.generate(request)
        if isinstance(result, ModelResponse):
            await callback(StreamTextDelta(text="hel"))
            await callback(StreamTextDelta(text="lo!"))
        return result


class RecordingUserSimulatorSession:
    """Sends the given texts as user turns with note metadata, records what it is shown."""

    def __init__(self, texts: tuple[str, ...]):
        self._texts = list(texts)
        self.seen: list[tuple[ChatMessage, ...] | None] = []

    async def next_user_turn(
        self,
        model_messages: tuple[ChatMessage, ...] | None,
        *,
        user_turn_override: UserMessage | None = None,
    ) -> UserTurn | EndRollout:
        if user_turn_override is not None:
            raise NotImplementedError
        self.seen.append(model_messages)
        if not self._texts:
            return EndRollout(metadata=_meta(Note("ran out of things to say")))
        return UserTurn(
            message=_user(self._texts.pop(0)), metadata=_meta(Note(f"note {len(self.seen)}"))
        )

    async def aclose(self) -> None:
        pass


class RecordingUserSimulator:
    def __init__(self, texts: tuple[str, ...]):
        self._texts = texts
        self.last_session: RecordingUserSimulatorSession | None = None

    async def start_session(self) -> RecordingUserSimulatorSession:
        self.last_session = RecordingUserSimulatorSession(self._texts)
        return self.last_session


class EchoToolSession:
    """Offers one tool and answers every call with "ok", counting calls in metadata."""

    async def available_tools(self) -> tuple[ToolParam, ...]:
        return (
            ToolParam(
                name="search",
                description=None,
                input_schema={"type": "object"},
                strict=None,
                input_examples=None,
            ),
        )

    async def generate_tool_turn(self, tool_uses: tuple[ContentToolUse, ...]) -> ToolTurn:
        return ToolTurn(
            messages=tuple(
                ToolMessage(content=(ContentText(text="ok"),), tool_call_id=use.id, is_error=False)
                for use in tool_uses
            ),
            metadata=_meta(Count(len(tool_uses))),
        )

    async def aclose(self) -> None:
        pass


class EchoToolPolicy:
    async def start_session(self) -> EchoToolSession:
        return EchoToolSession()


# Static checks that the concrete classes satisfy the protocols.
_model_api: ModelAPI = ScriptedModelAPI(())
_session_model: SerializableChatSessionModel = chat_session_model_from_api(
    ScriptedModelAPI(()), SamplingParams()
)
_streaming_session: StreamingChatSession = APIBackedChatSession(
    ScriptedModelAPI(()), SamplingParams(), None, (), ()
)
_streaming_model: SerializableStreamingChatSessionModel = chat_session_model_from_api(
    StreamingScriptedModelAPI(()), SamplingParams()
)
_user_simulator: UserSimulator = RecordingUserSimulator(())
_user_simulator_session: UserSimulatorSession = RecordingUserSimulatorSession(())
_scripted_simulator: SerializableUserSimulator = ScriptedUserSimulator(messages=())
_tool_policy: SerializableToolPolicy = NoTools()
_echo_tool_policy: ToolPolicy = EchoToolPolicy()


def test_rollout_with_user_simulator() -> None:
    model_api = ScriptedModelAPI(
        outputs=(_response(ContentText(text="hello!")), _response(ContentText(text="4")))
    )
    model = chat_session_model_from_api(model_api, SamplingParams(max_tokens=100))
    simulator = RecordingUserSimulator(texts=("hi", "what is 2+2?"))
    policy = context_policy_from_user_simulator(
        simulator,
        initialization=SetSystemMessage(
            message=SystemMessage(content=(ContentText(text="be helpful"),))
        ),
    )

    rollout = anyio.run(run_chat_rollout, model, policy)

    assert rollout.status == RolloutCompleted()
    assert rollout.end == EndRollout(metadata=_meta(Note("ran out of things to say")))
    assert rollout.initialization == DynamicData.from_tagged(
        SetSystemMessage(message=SystemMessage(content=(ContentText(text="be helpful"),)))
    )
    assert len(rollout.context_turns) == len(rollout.model_turns) == 2
    model_turn = rollout.model_turns[1]
    assert model_turn.messages[0].content == (ContentText(text="4"),)
    assert find_metadata(model_turn.metadata, ModelResponse) == _response(ContentText(text="4"))

    # The second request carries the full accumulated conversation.
    assert [type(m).__name__ for m in model_api.requests[1].context] == [
        "SystemMessage",
        "UserMessage",
        "AssistantMessage",
        "UserMessage",
    ]
    # The simulator sees None first, then the model's messages since its last turn.
    session = simulator.last_session
    assert session is not None
    assert session.seen[0] is None
    assert session.seen[1] is not None and len(session.seen[1]) == 1
    # The user turn's metadata rides on the context turn.
    assert rollout.context_turns[0].metadata == _meta(Note("note 1"))
    assert rollout.context_turns[0].available_tools is None


def test_scripted_simulator_override_replaces_the_next_message() -> None:
    async def run() -> None:
        session = await ScriptedUserSimulator(messages=(_user("one"), _user("two"))).start_session()
        assert await session.next_user_turn(None, user_turn_override=_user("instead")) == UserTurn(
            message=_user("instead")
        )
        assert await session.next_user_turn(None) == UserTurn(message=_user("two"))

    anyio.run(run)


def test_user_simulator_policy_rejects_tool_calls() -> None:
    tool_call = ContentToolUse(id="tu_1", name="search", input={"q": "cats"}, parse_error=None)
    model_api = ScriptedModelAPI(outputs=(_response(tool_call),))
    model = chat_session_model_from_api(model_api, SamplingParams())
    policy = context_policy_from_user_simulator(
        ScriptedUserSimulator(messages=(_user("hi"), _user("again"))), initialization=None
    )

    with pytest.raises(RuntimeError, match="tool calls"):
        anyio.run(run_chat_rollout, model, policy)


def test_rollout_agentic_tool_loop() -> None:
    tool_call = ContentToolUse(id="tu_1", name="search", input={"q": "cats"}, parse_error=None)
    model_api = ScriptedModelAPI(
        outputs=(_response(tool_call), _response(ContentText(text="all done")))
    )
    model = chat_session_model_from_api(model_api, SamplingParams())
    policy = context_policy_from_tool_policy(
        EchoToolPolicy(), initialization=None, first_turn_messages=(_user("find me cats"),)
    )

    rollout = anyio.run(run_chat_rollout, model, policy)

    # First turn is the task message with tools; the tool call is answered;
    # the tool-less model turn ends the rollout.
    assert rollout.status == RolloutCompleted()
    assert rollout.end == EndRollout()
    assert len(rollout.context_turns) == len(rollout.model_turns) == 2
    first = rollout.context_turns[0]
    assert first.messages == (_user("find me cats"),)
    assert first.available_tools is not None and first.available_tools[0].name == "search"
    tool_turn = rollout.context_turns[1]
    result = tool_turn.messages[0]
    assert isinstance(result, ToolMessage) and result.tool_call_id == "tu_1"
    assert tool_turn.metadata == _meta(Count(1))


def test_rollout_tool_call_loop() -> None:
    tool_call = ContentToolUse(id="tu_1", name="search", input={"q": "cats"}, parse_error=None)
    model_api = ScriptedModelAPI(
        outputs=(_response(tool_call), _response(ContentText(text="no results, sorry")))
    )
    model = chat_session_model_from_api(model_api, SamplingParams())
    policy = context_policy_from_user_and_tools(
        ScriptedUserSimulator(messages=(_user("find me cats"),)),
        NoTools(),
        initialization=None,
    )

    rollout = anyio.run(run_chat_rollout, model, policy)

    # One user turn, one tool-results turn, then the simulator's second
    # consultation ends the rollout.
    assert rollout.status == RolloutCompleted()
    assert rollout.end == EndRollout()
    assert len(rollout.context_turns) == 2
    tool_result = rollout.context_turns[1].messages[0]
    assert isinstance(tool_result, ToolMessage)
    assert tool_result.is_error and tool_result.tool_call_id == "tu_1"
    assert rollout.context_turns[0].available_tools == ()


def test_rollout_records_context_length_failure() -> None:
    failure = ModelCallFailure(
        category="context_length", description="prompt is too long", provider_http_status=400
    )
    model_api = ScriptedModelAPI(outputs=(_response(ContentText(text="hello!")), failure))
    model = chat_session_model_from_api(model_api, SamplingParams())
    initialization = SetSystemMessage(SystemMessage((ContentText("failure context"),)))
    policy = context_policy_from_user_simulator(
        RecordingUserSimulator(texts=("hi", "tell me more", "even more")),
        initialization=initialization,
    )

    rollout = anyio.run(run_chat_rollout, model, policy)

    assert rollout.status == RolloutFailed(failure=failure)
    assert rollout.initialization == DynamicData.from_tagged(initialization)
    assert rollout.end is None
    # The unanswered context turn that triggered the failure is preserved.
    assert len(rollout.context_turns) == len(rollout.model_turns) + 1 == 2


def test_rollout_raises_on_unrecorded_failure_category() -> None:
    failure = ModelCallFailure(category="rate_limited", description="429", provider_http_status=429)
    model_api = ScriptedModelAPI(outputs=(failure,))
    model = chat_session_model_from_api(model_api, SamplingParams())
    policy = context_policy_from_user_simulator(
        ScriptedUserSimulator(messages=(_user("hi"),)), initialization=None
    )

    with pytest.raises(ModelCallFailedError) as excinfo:
        anyio.run(run_chat_rollout, model, policy)
    assert excinfo.value.failure == failure


def test_bridge_retries_configured_categories() -> None:
    failure = ModelCallFailure(category="rate_limited", description="429", provider_http_status=429)
    model_api = ScriptedModelAPI(outputs=(failure, failure, _response(ContentText(text="hello!"))))
    model = chat_session_model_from_api(
        model_api,
        SamplingParams(),
        retry=RetryConfig(max_attempts=3, initial_backoff_seconds=0.0, jitter_seconds=0.0),
        failure_categories_to_retry=("rate_limited",),
    )
    policy = context_policy_from_user_simulator(
        ScriptedUserSimulator(messages=(_user("hi"),)), initialization=None
    )

    rollout = anyio.run(run_chat_rollout, model, policy)

    assert rollout.status == RolloutCompleted()
    assert len(model_api.requests) == 3
    # Retries resend the same request rather than growing the conversation.
    assert model_api.requests[0].context == model_api.requests[2].context


def test_bridge_returns_failure_when_retries_exhausted() -> None:
    failure = ModelCallFailure(category="rate_limited", description="429", provider_http_status=429)
    model_api = ScriptedModelAPI(outputs=(failure, failure))
    model = chat_session_model_from_api(
        model_api,
        SamplingParams(),
        retry=RetryConfig(max_attempts=2, initial_backoff_seconds=0.0, jitter_seconds=0.0),
        failure_categories_to_retry=("rate_limited",),
    )
    policy = context_policy_from_user_simulator(
        ScriptedUserSimulator(messages=(_user("hi"),)), initialization=None
    )

    with pytest.raises(ModelCallFailedError):
        anyio.run(run_chat_rollout, model, policy)


def test_bridge_snapshot_and_restore() -> None:
    async def go() -> None:
        model_api = ScriptedModelAPI(
            outputs=(
                _response(ContentText(text="hello!")),
                _response(ContentText(text="again")),
            )
        )
        model = chat_session_model_from_api(model_api, SamplingParams())
        session = await model.start_session(None)
        await session.generate_turn(ContextTurn(messages=(_user("hi"),), available_tools=None))
        state = _json_roundtrip(await session.get_state())

        restored = await model.restore_session(state)
        assert await restored.get_state() == state
        turn = await restored.generate_turn(
            ContextTurn(messages=(_user("more"),), available_tools=None)
        )
        assert isinstance(turn, ModelTurn)
        # The restored session carries the whole prior conversation into its request.
        assert [type(m).__name__ for m in model_api.requests[1].context] == [
            "UserMessage",
            "AssistantMessage",
            "UserMessage",
        ]

    anyio.run(go)


def test_bridge_initial_messages_precede_policy_initialization() -> None:
    async def go() -> None:
        baked_system = SystemMessage(content=(ContentText(text="baked"),))
        example = _user("example")
        model_api = ScriptedModelAPI(
            outputs=(_response(ContentText(text="a")), _response(ContentText(text="b")))
        )
        model = chat_session_model_from_api(
            model_api, SamplingParams(), initial_messages=(baked_system, example)
        )
        session = await model.start_session(None)
        await session.generate_turn(ContextTurn(messages=(_user("hi"),), available_tools=None))
        assert model_api.requests[0].context[:2] == (baked_system, example)

        # Restoring doesn't re-apply the initial messages; the state already has them.
        restored = await model.restore_session(await session.get_state())
        await restored.generate_turn(ContextTurn(messages=(_user("more"),), available_tools=None))
        assert model_api.requests[1].context[:2] == (baked_system, example)
        assert len(model_api.requests[1].context) == 5

        policy_system = SystemMessage(content=(ContentText(text="from policy"),))
        policy_api = ScriptedModelAPI(outputs=(_response(ContentText(text="c")),))
        with_policy = chat_session_model_from_api(
            policy_api, SamplingParams(), initial_messages=(example,)
        )
        started = await with_policy.start_session(SetSystemMessage(message=policy_system))
        await started.generate_turn(ContextTurn(messages=(_user("hi"),), available_tools=None))
        assert policy_api.requests[0].context[:2] == (example, policy_system)

    anyio.run(go)


def test_bridge_rejects_a_second_system_message() -> None:
    model = chat_session_model_from_api(
        ScriptedModelAPI(outputs=()),
        SamplingParams(),
        initial_messages=(SystemMessage(content=(ContentText(text="baked"),)),),
    )
    initialization = SetSystemMessage(
        message=SystemMessage(content=(ContentText(text="from policy"),))
    )
    with pytest.raises(ModelError):
        anyio.run(model.start_session, initialization)


def test_bridge_streaming_turn_with_retry() -> None:
    failure = ModelCallFailure(category="rate_limited", description="429", provider_http_status=429)
    session = APIBackedChatSession(
        StreamingScriptedModelAPI(outputs=(failure, _response(ContentText(text="hello!")))),
        SamplingParams(),
        RetryConfig(max_attempts=2, initial_backoff_seconds=0.0, jitter_seconds=0.0),
        ("rate_limited",),
        (),
    )

    async def go() -> None:
        events: list[StreamEvent] = []

        async def callback(event: StreamEvent) -> None:
            events.append(event)

        turn = await session.generate_turn_with_events(
            ContextTurn(messages=(_user("hi"),), available_tools=None), callback
        )
        assert isinstance(turn, ModelTurn)
        assert events == [StreamRestart(), StreamTextDelta(text="hel"), StreamTextDelta(text="lo!")]

    anyio.run(go)


def test_serializable_combined_policy_snapshot_and_restore() -> None:
    async def go() -> None:
        policy = serializable_context_policy_from_user_and_tools(
            ScriptedUserSimulator(messages=(_user("one"), _user("two"))),
            NoTools(),
            initialization=None,
        )
        session = await policy.start_session()
        first = await session.generate_context_turn(None)
        assert isinstance(first, ContextTurn)
        state = _json_roundtrip(await session.get_state())

        model_turn = ModelTurn(messages=(AssistantMessage(content=(ContentText(text="ok"),)),))
        second = await session.generate_context_turn(model_turn)
        restored_second = await (await policy.restore_session(state)).generate_context_turn(
            model_turn
        )

        assert second == restored_second
        assert isinstance(second, ContextTurn)
        assert second.messages == (_user("two"),)

    anyio.run(go)


def test_rollout_event_callback() -> None:
    events: list[ChatRolloutEvent] = []

    async def record(event: ChatRolloutEvent) -> None:
        events.append(event)

    model_api = StreamingScriptedModelAPI(outputs=(_response(ContentText(text="hello!")),))
    model = chat_session_model_from_api(model_api, SamplingParams())
    policy = context_policy_from_user_simulator(
        ScriptedUserSimulator(messages=(_user("hi"),)), initialization=None
    )

    rollout = anyio.run(functools.partial(run_chat_rollout, model, policy, event_callback=record))

    # The streaming session's deltas arrive between the turn that prompted
    # them and the assembled model turn.
    assert [type(event).__name__ for event in events] == [
        "ContextTurnEvent",
        "ModelStreamEvent",
        "ModelStreamEvent",
        "ModelTurnEvent",
    ]
    # Events carry the same objects that land on the rollout.
    assert isinstance(events[0], ContextTurnEvent)
    assert events[0].turn is rollout.context_turns[0]
    assert isinstance(events[1], ModelStreamEvent)
    assert events[1].event == StreamTextDelta(text="hel")
    assert isinstance(events[3], ModelTurnEvent)
    assert events[3].turn is rollout.model_turns[0]


def test_generator_context_policy() -> None:
    closed = False

    async def policy_gen() -> AsyncGenerator[ContextTurn | EndRollout, ModelTurn | None]:
        nonlocal closed
        try:
            model_turn = yield ContextTurn(messages=(_user("hi"),), available_tools=None)
            assert model_turn is not None
            yield EndRollout(metadata=_meta(Count(len(model_turn.messages))))
        finally:
            closed = True

    model_api = ScriptedModelAPI(outputs=(_response(ContentText(text="hello!")),))
    model = chat_session_model_from_api(model_api, SamplingParams())
    policy = context_policy_from_generator(policy_gen, initialization=None)

    rollout = anyio.run(run_chat_rollout, model, policy)

    assert rollout.status == RolloutCompleted()
    assert rollout.end == EndRollout(metadata=_meta(Count(1)))
    assert len(rollout.context_turns) == len(rollout.model_turns) == 1
    assert closed


def test_generator_context_policy_base_class() -> None:
    @dataclasses.dataclass(frozen=True)
    class OpeningPolicy(GeneratorContextPolicy):
        opening: str

        async def generate_initialization(self) -> SessionInitialization | None:
            return SetSystemMessage(
                message=SystemMessage(content=(ContentText(text="be helpful"),))
            )

        async def generate_turns(
            self,
        ) -> AsyncGenerator[ContextTurn | EndRollout, ModelTurn | None]:
            model_turn = yield ContextTurn(messages=(_user(self.opening),), available_tools=None)
            assert model_turn is not None
            yield EndRollout(metadata=_meta(Count(len(model_turn.messages))))

    policy = OpeningPolicy(opening="hi")
    # One policy instance serves many sessions.
    for _ in range(2):
        model_api = ScriptedModelAPI(outputs=(_response(ContentText(text="hello!")),))
        model = chat_session_model_from_api(model_api, SamplingParams())

        rollout = anyio.run(run_chat_rollout, model, policy)

        assert rollout.status == RolloutCompleted()
        assert rollout.end == EndRollout(metadata=_meta(Count(1)))
        assert [type(m).__name__ for m in model_api.requests[0].context] == [
            "SystemMessage",
            "UserMessage",
        ]


def test_generator_context_policy_must_yield_end() -> None:
    async def policy_gen() -> AsyncGenerator[ContextTurn | EndRollout, ModelTurn | None]:
        yield ContextTurn(messages=(_user("hi"),), available_tools=None)

    model_api = ScriptedModelAPI(outputs=(_response(ContentText(text="hello!")),))
    model = chat_session_model_from_api(model_api, SamplingParams())
    policy = context_policy_from_generator(policy_gen, initialization=None)

    with pytest.raises(RuntimeError, match="EndRollout"):
        anyio.run(run_chat_rollout, model, policy)


@pytest.mark.anyio
async def test_bridge_failure_preserves_state_and_retry_context() -> None:
    failure = ModelCallFailure(
        category="context_length", description="too long", provider_http_status=400
    )
    model_api = ScriptedModelAPI(outputs=(failure, _response(ContentText(text="ok"))))
    model = chat_session_model_from_api(model_api, SamplingParams())
    session = await model.start_session(
        SetSystemMessage(message=SystemMessage(content=(ContentText(text="system"),)))
    )
    state = await session.get_state()

    assert (
        await session.generate_turn(
            ContextTurn(messages=(_user("too much context"),), available_tools=None)
        )
        == failure
    )
    assert await session.get_state() == state
    await session.generate_turn(ContextTurn(messages=(_user("short"),), available_tools=None))

    assert model_api.requests[1].context == (
        SystemMessage(content=(ContentText(text="system"),)),
        _user("short"),
    )


@pytest.mark.anyio
async def test_snapshots_do_not_alias_session_messages() -> None:
    tool_call = ContentToolUse(
        id="tool_1", name="search", input={"nested": {"query": ["cats"]}}, parse_error=None
    )
    model = chat_session_model_from_api(
        ScriptedModelAPI(outputs=(_response(tool_call),)), SamplingParams()
    )
    session = await model.start_session(None)
    await session.generate_turn(ContextTurn(messages=(_user("hi"),), available_tools=None))
    state = await session.get_state()
    original = _json_roundtrip(state)
    # Deliberately mutate a nested structure the session's messages share.
    tool_call.input["nested"] = "changed outside the snapshot"

    assert state == original
    restored = await model.restore_session(original)
    assert await restored.get_state() == original


@pytest.mark.anyio
async def test_combined_snapshot_preserves_pending_tool_conversation() -> None:
    policy = serializable_context_policy_from_user_and_tools(
        ScriptedUserSimulator(messages=(_user("one"), _user("two"))),
        NoTools(),
        initialization=None,
        show_tool_calls_to_user=True,
    )
    session = await policy.start_session()
    await session.generate_context_turn(None)
    tool_call = ContentToolUse(id="tool_1", name="search", input={"q": "cats"}, parse_error=None)
    await session.generate_context_turn(
        ModelTurn(messages=(AssistantMessage(content=(tool_call,)),))
    )
    state = _json_roundtrip(await session.get_state())
    restored = await policy.restore_session(state)
    assert await restored.get_state() == state
    assert len(_state_data(state)["pending_for_user"]) == 2

    turn = ModelTurn(messages=(AssistantMessage(content=(ContentText(text="done"),)),))
    assert await session.generate_context_turn(turn) == await restored.generate_context_turn(turn)
    assert await session.get_state() == await restored.get_state()
    # Advancing either session does not change the captured state.
    assert len(_state_data(state)["pending_for_user"]) == 2


@pytest.mark.parametrize("show_tool_calls", [False, True])
def test_combined_policy_controls_tool_visibility(show_tool_calls: bool) -> None:
    tool_call = ContentToolUse(id="tool_1", name="search", input={}, parse_error=None)
    first_reply = _response(ContentText(text="searching"), tool_call)
    last_reply = _response(ContentText(text="done"))
    model = chat_session_model_from_api(
        ScriptedModelAPI(outputs=(first_reply, last_reply)), SamplingParams()
    )
    simulator = RecordingUserSimulator(("find it",))
    policy = context_policy_from_user_and_tools(
        simulator, NoTools(), initialization=None, show_tool_calls_to_user=show_tool_calls
    )

    anyio.run(run_chat_rollout, model, policy)

    assert simulator.last_session is not None
    seen = simulator.last_session.seen[1]
    assert seen is not None
    if show_tool_calls:
        assert len(seen) == 3
        assert seen[0] == first_reply.output[0]
        assert isinstance(seen[1], ToolMessage)
    else:
        assert seen == (
            AssistantMessage(content=(ContentText(text="searching"),)),
            last_reply.output[0],
        )


@pytest.mark.anyio
async def test_rollout_closes_sessions_after_cancellation() -> None:
    closed: list[str] = []
    generating = anyio.Event()

    class PolicySession:
        async def generate_initialization(self) -> None:
            return None

        async def generate_context_turn(self, model_turn: ModelTurn | None) -> ContextTurn:
            return ContextTurn(messages=(_user("hi"),), available_tools=None)

        async def aclose(self) -> None:
            await anyio.sleep(0)
            closed.append("policy")

    class Policy:
        async def start_session(self) -> PolicySession:
            return PolicySession()

    class ModelSession:
        async def generate_turn(self, context_turn: ContextTurn) -> ModelTurn:
            generating.set()
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        async def aclose(self) -> None:
            await anyio.sleep(0)
            closed.append("model")

    class Model:
        async def start_session(self, initialization: SessionInitialization | None) -> ModelSession:
            return ModelSession()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run_chat_rollout, Model(), Policy())
        await generating.wait()
        tasks.cancel_scope.cancel()

    assert closed == ["model", "policy"]


@pytest.mark.parametrize("failure_point", ["initialize", "start_model", "callback"])
@pytest.mark.anyio
async def test_rollout_closes_opened_sessions_after_errors(failure_point: str) -> None:
    closed: list[str] = []

    class PolicySession:
        async def generate_initialization(self) -> None:
            if failure_point == "initialize":
                raise RuntimeError(failure_point)
            return None

        async def generate_context_turn(self, model_turn: ModelTurn | None) -> ContextTurn:
            return ContextTurn(messages=(_user("hi"),), available_tools=None)

        async def aclose(self) -> None:
            closed.append("policy")

    class Policy:
        async def start_session(self) -> PolicySession:
            return PolicySession()

    class ModelSession:
        async def generate_turn(self, context_turn: ContextTurn) -> ModelTurn:
            raise AssertionError("The callback should fail before generation")

        async def aclose(self) -> None:
            closed.append("model")

    class Model:
        async def start_session(self, initialization: SessionInitialization | None) -> ModelSession:
            if failure_point == "start_model":
                raise RuntimeError(failure_point)
            return ModelSession()

    async def callback(event: ChatRolloutEvent) -> None:
        raise RuntimeError(failure_point)

    with pytest.raises(RuntimeError, match=failure_point):
        await run_chat_rollout(Model(), Policy(), event_callback=callback)
    assert closed == (["model", "policy"] if failure_point == "callback" else ["policy"])


@pytest.mark.parametrize("operation", ["start", "serializable_start", "restore"])
@pytest.mark.anyio
async def test_combined_policy_closes_user_if_tool_acquisition_fails(operation: str) -> None:
    closed: list[str] = []

    class UserSession(RecordingUserSimulatorSession):
        async def get_state(self) -> DynamicData:
            return DynamicData.from_tagged(Note("user"))

        async def aclose(self) -> None:
            await anyio.sleep(0)
            closed.append("user")

    class User:
        async def start_session(self) -> UserSession:
            return UserSession(())

        async def restore_session(self, state: DynamicData) -> UserSession:
            return UserSession(())

    class Tools:
        async def start_session(self) -> SerializableToolPolicySession:
            raise RuntimeError("tool acquisition failed")

        async def restore_session(self, state: DynamicData) -> SerializableToolPolicySession:
            raise RuntimeError("tool acquisition failed")

    with pytest.raises(RuntimeError, match="tool acquisition failed"):
        if operation == "start":
            await context_policy_from_user_and_tools(
                User(), Tools(), initialization=None
            ).start_session()
        else:
            policy = serializable_context_policy_from_user_and_tools(
                User(), Tools(), initialization=None
            )
            if operation == "serializable_start":
                await policy.start_session()
            else:
                working = serializable_context_policy_from_user_and_tools(
                    ScriptedUserSimulator(messages=()), NoTools(), initialization=None
                )
                state = await (await working.start_session()).get_state()
                await policy.restore_session(state)
    assert closed == ["user"]


@pytest.mark.parametrize("max_turns", [0, 1])
@pytest.mark.anyio
async def test_rollout_turn_limit(max_turns: int) -> None:
    api = ScriptedModelAPI(outputs=(_response(ContentText(text="ok")),))
    initialization = SetSystemMessage(SystemMessage((ContentText("limited context"),)))
    policy = context_policy_from_user_simulator(
        ScriptedUserSimulator(messages=(_user("one"), _user("two"))),
        initialization=initialization,
    )
    rollout = await run_chat_rollout(
        chat_session_model_from_api(api, SamplingParams()), policy, max_turns=max_turns
    )
    assert rollout.status == RolloutMaxTurns(max_turns=max_turns)
    assert rollout.initialization == DynamicData.from_tagged(initialization)
    assert rollout.end is None
    assert len(rollout.model_turns) == len(rollout.context_turns) == len(api.requests) == max_turns


@pytest.mark.parametrize("corruption", ["missing_field", "invalid_field", "extra_key"])
@pytest.mark.anyio
async def test_sessions_reject_corrupt_checkpoints(corruption: str) -> None:
    model = chat_session_model_from_api(ScriptedModelAPI(outputs=()), SamplingParams())
    simulator = ScriptedUserSimulator(messages=(_user("hello"),))
    policy = serializable_context_policy_from_user_and_tools(
        ScriptedUserSimulator(messages=()), NoTools(), initialization=None
    )
    sessions = (
        (model, await model.start_session(None), "messages"),
        (simulator, await simulator.start_session(), "messages"),
        (policy, await policy.start_session(), "pending_for_user"),
    )
    for factory, session, field in sessions:
        data = _state_data(await session.get_state())
        if corruption == "missing_field":
            del data[field]
        elif corruption == "invalid_field":
            data[field] = "invalid"
        else:
            data["extra"] = 1
        with pytest.raises(ConversionError):
            await factory.restore_session(DynamicData.from_data(data))
        await session.aclose()


@pytest.mark.anyio
async def test_sessions_reject_other_implementations_state() -> None:
    model = chat_session_model_from_api(ScriptedModelAPI(outputs=()), SamplingParams())
    simulator = ScriptedUserSimulator(messages=())
    model_state = await (await model.start_session(None)).get_state()
    simulator_state = await (await simulator.start_session()).get_state()
    with pytest.raises(ConversionError):
        await model.restore_session(simulator_state)
    with pytest.raises(ConversionError):
        await simulator.restore_session(model_state)
    with pytest.raises(ConversionError):
        await NoTools().restore_session(model_state)


@pytest.mark.anyio
async def test_no_tools_checkpoint_roundtrip() -> None:
    state = await (await NoTools().start_session()).get_state()
    session = await NoTools().restore_session(_json_roundtrip(state))
    assert await session.available_tools() == ()
    assert await session.get_state() == state


@pytest.mark.anyio
async def test_rollout_rejects_negative_turn_limit() -> None:
    model = chat_session_model_from_api(ScriptedModelAPI(outputs=()), SamplingParams())
    policy = context_policy_from_user_simulator(
        ScriptedUserSimulator(messages=()), initialization=None
    )
    with pytest.raises(ValueError, match="nonnegative"):
        await run_chat_rollout(model, policy, max_turns=-1)
