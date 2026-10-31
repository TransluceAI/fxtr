"""Unit tests for the pilot-steered user simulator, with scripted pilot and base simulators.

The base is usually the base-model simulator over a scripted completions client, so
candidates carry raw text and end markers the way they do in practice; ``ScriptedUserSimulator``
serves where identical candidates are the point. The check that pilot prompts match ABR's
byte-for-byte lives in ABR's tests.
"""

import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import anyio
import pytest
from openai import AsyncOpenAI

from behaviors.implementations.context_policies import ScriptedUserSimulator, SimulatedUserText
from behaviors.implementations.context_policies.base_model_prompt import SimulatedUserProfile
from behaviors.implementations.context_policies.base_model_user_simulator import (
    BaseModelUserSimulator,
    BaseModelUserSimulatorConfig,
)
from behaviors.implementations.context_policies.pilot_prompt import (
    PilotResponse,
    augmented_shifts_for_selection,
    parse_pilot_response,
    render_pilot_system_message,
    softmax_probabilities,
)
from behaviors.implementations.context_policies.pilot_steered_user_simulator import (
    PILOT_RESPONSE_MAX_ATTEMPTS,
    PilotSteeredUserSimulator,
    PilotSteeredUserSimulatorConfig,
    PilotSteering,
    _SessionScope,  # pyright: ignore[reportPrivateUsage]
)
from behaviors.interfaces.context_policies import (
    SerializableUserSimulator,
    SerializableUserSimulatorSession,
)
from behaviors.types.content import (
    CacheableContentText,
    Content,
    ContentSummarizedReasoning,
    ContentText,
)
from behaviors.types.message import AssistantMessage, ChatMessage, SystemMessage, UserMessage
from behaviors.types.response import (
    ModelRequest,
    ModelResponse,
    ReasoningParams,
    SamplingParams,
    StopEndTurn,
    Usage,
)
from behaviors.types.rollout import EndRollout, UserTurn, find_metadata
from fxtr.core.entity_data import thaw_fxtr_data
from fxtr.entity_defns.dataclass_bases import TaggedDataclass
from fxtr.entity_defns.dynamic_data import DynamicData


class _Choice:
    def __init__(self, text: str) -> None:
        self.text = text


class _Chunk:
    def __init__(self, text: str) -> None:
        self.choices = [_Choice(text)]


class _ScriptedStream:
    def __init__(self, delta_texts: list[str]) -> None:
        self._delta_texts = delta_texts

    async def __aenter__(self) -> "_ScriptedStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[_Chunk]:
        return self._chunks()

    async def _chunks(self) -> AsyncIterator[_Chunk]:
        for text in self._delta_texts:
            yield _Chunk(text)


class _ScriptedCompletions:
    """Serves one scripted delta stream per ``create`` call, recording each call's kwargs."""

    def __init__(self, streams: list[list[str]]) -> None:
        self._streams = streams
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _ScriptedStream:
        self.calls.append(kwargs)
        return _ScriptedStream(self._streams[len(self.calls) - 1])


class _Client:
    def __init__(self, streams: list[list[str]]) -> None:
        self.completions = _ScriptedCompletions(streams)


class ScriptedPilot:
    """A ``ModelAPI`` answering each request with the next scripted item, recording requests.

    An item is the response text, or a ``(thinking, text)`` pair.
    """

    def __init__(self, items: list[str | tuple[str, str]]) -> None:
        self._items = items
        self.requests: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        item = self._items[len(self.requests) - 1]
        thinking, text = item if isinstance(item, tuple) else (None, item)
        content: list[Content] = []
        if thinking is not None:
            content.append(ContentSummarizedReasoning(text=thinking, signature=None))
        content.append(ContentText(text=text))
        return ModelResponse(
            id="msg",
            output=(AssistantMessage(content=tuple(content)),),
            stop_reason=StopEndTurn(),
            usage=Usage(
                input_tokens=1,
                cache_read_input_tokens=None,
                cache_write_input_tokens=None,
                output_tokens=1,
                reasoning_output_tokens=None,
            ),
        )


_PROFILE = SimulatedUserProfile(user_id="u", transcript_id="t", user_bio="Terse.")
_SUPPRESS = -1000.0  # a shift that makes a candidate's selection probability round to zero


def _user(text: str) -> UserMessage:
    return UserMessage(content=(ContentText(text=text),))


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=(ContentText(text=text),))


def _text(message: ChatMessage) -> str:
    return "".join(block.text for block in message.content if isinstance(block, ContentText))


def _yaml(shifts: list[float], custom_text: str | None = None) -> str:
    return json.dumps({"shifts": shifts, "custom_text": custom_text})


def _base(completions: list[list[str]]) -> tuple[BaseModelUserSimulator, _ScriptedCompletions]:
    client = _Client(completions)
    config = BaseModelUserSimulatorConfig(profile=_PROFILE, model="base-model")
    return BaseModelUserSimulator(config, client=cast(AsyncOpenAI, client)), client.completions


def _steered(
    base: SerializableUserSimulator,
    pilot_items: list[str | tuple[str, str]],
    *,
    allow_custom_text: bool = True,
    steer_first_turn: bool = True,
) -> tuple[PilotSteeredUserSimulator, ScriptedPilot]:
    pilot = ScriptedPilot(pilot_items)
    config = PilotSteeredUserSimulatorConfig(
        user_bio="Terse.",
        pilot_instructions="Stay terse.",
        candidate_count=2,
        allow_custom_text=allow_custom_text,
        steer_first_turn=steer_first_turn,
        random_seed=0,
    )
    return PilotSteeredUserSimulator(base, pilot, config), pilot


def _steering(turn: UserTurn | EndRollout) -> PilotSteering:
    record = find_metadata(turn.metadata, PilotSteering)
    assert record is not None
    return record


def _source(turn: UserTurn | EndRollout) -> str | None:
    text = find_metadata(turn.metadata, SimulatedUserText)
    return None if text is None else text.source


def _raw_text(turn: UserTurn | EndRollout) -> str | None:
    text = find_metadata(turn.metadata, SimulatedUserText)
    return None if text is None else text.raw_text


def _json_roundtrip(value: DynamicData) -> DynamicData:
    return DynamicData.from_data(json.loads(json.dumps(thaw_fxtr_data(value.data))))


def test_conforms_to_serializable_user_simulator() -> None:
    steered, _ = _steered(_base([])[0], [])
    assert isinstance(steered, SerializableUserSimulator)


def test_parse_pilot_response() -> None:
    parsed = parse_pilot_response(
        "```yaml\nshifts: [0, 1.5]\ncustom_text: '  '\n```", expected_candidate_count=2
    )
    assert parsed == PilotResponse(shifts=(0.0, 1.5), custom_text=None)
    assert parse_pilot_response(_yaml([0, 0], " hi "), expected_candidate_count=2) == PilotResponse(
        shifts=(0.0, 0.0), custom_text="hi"
    )
    for malformed in (
        "not: [valid",
        "[1, 2]",
        _yaml([0]),
        '{"shifts": ["a", "b"]}',
        "shifts: [.nan, 0]",
        "shifts: [-.inf, 0]",
    ):
        with pytest.raises(ValueError):
            parse_pilot_response(malformed, expected_candidate_count=2)
    with pytest.raises(ValueError):
        parse_pilot_response(
            _yaml([0, 0], "mine"), expected_candidate_count=2, allow_custom_text=False
        )
    assert parse_pilot_response(
        _yaml([0, 0], "  "), expected_candidate_count=2, allow_custom_text=False
    ) == PilotResponse(shifts=(0.0, 0.0), custom_text=None)


def test_custom_text_takes_a_final_slot_ten_above_the_top_shift() -> None:
    plain = PilotResponse(shifts=(1.0, -2.0))
    assert augmented_shifts_for_selection(plain, candidate_count=2) == (1.0, -2.0)
    custom = PilotResponse(shifts=(1.0, -2.0), custom_text="mine")
    assert augmented_shifts_for_selection(custom, candidate_count=2) == (1.0, -2.0, 11.0)
    probabilities = softmax_probabilities((1.0, -2.0, 11.0))
    assert sum(probabilities) == pytest.approx(1.0)  # pyright: ignore[reportUnknownMemberType]
    assert probabilities[2] > 0.99


def test_system_message_reflects_custom_text_setting() -> None:
    enabled = render_pilot_system_message(allow_custom_text=True, candidate_count=3)
    disabled = render_pilot_system_message(allow_custom_text=False, candidate_count=3)
    assert 'You also have a "custom text" capability' in enabled
    assert 'You do **not** have the "custom text" capability' in disabled
    assert "x_0, ..., x_2" in enabled


def test_pilot_chooses_among_candidates_from_restored_base_sessions() -> None:
    async def run() -> None:
        base, completions = _base([["alpha"], ["beta"], ["gamma"], ["delta"]])
        steered, pilot = _steered(
            base, [("pick beta", _yaml([_SUPPRESS, 0])), _yaml([0, _SUPPRESS])]
        )
        session = await steered.start_session()

        turn = await session.next_user_turn(None)
        assert isinstance(turn, UserTurn) and turn.message == _user("beta")
        assert _source(turn) == "sampled" and _raw_text(turn) == "beta"
        record = _steering(turn)
        assert record.transcript_length == 0
        assert record.candidates == ("alpha", "beta")
        assert record.shifts == (_SUPPRESS, 0.0)
        assert record.custom_text is None
        assert record.selected_index == 1 and record.selected_text == "beta"
        assert record.thinking == "pick beta"
        persisted = tuple(_json_roundtrip(entry) for entry in turn.metadata)
        assert find_metadata(persisted, PilotSteering) == record
        assert record.raw_response == _yaml([_SUPPRESS, 0])
        assert len(record.pilot_messages) == 2

        [request] = pilot.requests
        system, user = request.context
        assert isinstance(system, SystemMessage) and isinstance(user, UserMessage)
        assert _text(system) == render_pilot_system_message(
            allow_custom_text=True, candidate_count=2
        )
        assert all(
            isinstance(block, CacheableContentText) and block.cache_control is not None
            for block in (*system.content, *user.content)
        )
        first_user_content = _text(user)
        for expected in (
            "<user_bio>\nTerse.\n</user_bio>",
            "<pilot_instructions>\nStay terse.\n</pilot_instructions>",
            "(empty transcript)",
            '<candidate index="0">\nalpha\n</candidate>\n<candidate index="1">\nbeta\n</candidate>',
        ):
            assert expected in first_user_content
        assert request.sampling == SamplingParams(
            max_tokens=8192, reasoning=ReasoningParams(budget_tokens=4096)
        )

        reply = _assistant("Hi!")
        turn = await session.next_user_turn((reply,))
        assert isinstance(turn, UserTurn) and turn.message == _user("gamma")
        assert _steering(turn).transcript_length == 2
        # The kept base session is the one that said "beta".
        assert (
            "<|im_start|>user\nbeta<|im_end|>\n<|im_start|>assistant\nHi!<|im_end|>"
            in completions.calls[2]["prompt"]
        )
        # The pilot sees its own history, then only what changed.
        context = pilot.requests[1].context
        assert [message.role for message in context] == ["system", "user", "assistant", "user"]
        assert _text(context[1]) == first_user_content
        assert _text(context[2]) == _yaml([_SUPPRESS, 0])
        followup = _text(context[3])
        for expected in (
            "<last_steering_outcome>",
            "selected candidate index: 1",
            "<new_messages>",
            "Hi!",
            "There have been 2 message(s) in the chat so far, of which 1 have been user",
        ):
            assert expected in followup
        assert "<user_bio>" not in followup

    anyio.run(run)


def test_bare_end_marker_custom_text_ends_the_rollout() -> None:
    async def run() -> None:
        base, completions = _base([["alpha"], ["beta"]])
        steered, _ = _steered(base, [_yaml([0, 0], "</transcript>")])
        session = await steered.start_session()

        turn = await session.next_user_turn(None)
        assert isinstance(turn, EndRollout)
        record = _steering(turn)
        assert record.selected_index == 2 and record.selected_text == "</transcript>"
        assert len(completions.calls) == 2

    anyio.run(run)


def test_custom_text_is_sent_to_the_base_as_an_override() -> None:
    async def run() -> None:
        base, completions = _base([["alpha"], ["beta"], ["gamma"], ["delta"]])
        steered, _ = _steered(base, [_yaml([0, 0], "Actually, cats."), _yaml([0, _SUPPRESS])])
        session = await steered.start_session()

        turn = await session.next_user_turn(None)
        assert isinstance(turn, UserTurn) and turn.message == _user("Actually, cats.")
        assert _source(turn) == "override"
        record = _steering(turn)
        assert record.custom_text == "Actually, cats." and record.selected_index == 2

        await session.next_user_turn((_assistant("Cats it is."),))
        assert "<|im_start|>user\nActually, cats.<|im_end|>" in completions.calls[2]["prompt"]

    anyio.run(run)


def test_selected_text_containing_the_marker_ends_the_rollout_immediately() -> None:
    async def run() -> None:
        # ... whether the marker came from a base candidate or from the pilot's custom text.
        for completions_script, pilot_response, base_raw_text in (
            ([["bye</transcript>"], ["stay"]], _yaml([0, _SUPPRESS]), "bye</transcript>"),
            ([["alpha"], ["beta"]], _yaml([0, 0], "bye</transcript>"), None),
        ):
            base, completions = _base(completions_script)
            steered, pilot = _steered(base, [pilot_response])
            session = await steered.start_session()

            turn = await session.next_user_turn(None)
            assert isinstance(turn, EndRollout)
            assert _steering(turn).selected_text == "bye</transcript>"
            # A base candidate's own end keeps the base's metadata next to the steering record.
            assert _raw_text(turn) == base_raw_text
            assert len(completions.calls) == 2 and len(pilot.requests) == 1

    anyio.run(run)


@dataclasses.dataclass(frozen=True)
class _Origin(TaggedDataclass, fxtr_type="test.behaviors.Origin.v1"):
    name: str


@dataclasses.dataclass(frozen=True)
class _AnnotatingState(TaggedDataclass, fxtr_type="test.behaviors.AnnotatingState.v1"):
    pass


class _AnnotatingBase:
    """A generic base whose turns say the given text and carry their own metadata."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def start_session(self) -> "_AnnotatingSession":
        return _AnnotatingSession(self._text)

    async def restore_session(self, state: DynamicData) -> "_AnnotatingSession":
        state.as_type(_AnnotatingState)
        return _AnnotatingSession(self._text)


class _AnnotatingSession:
    def __init__(self, text: str) -> None:
        self._text = text

    async def next_user_turn(
        self,
        model_messages: tuple[ChatMessage, ...] | None,
        *,
        user_turn_override: UserMessage | None = None,
    ) -> UserTurn | EndRollout:
        return UserTurn(
            message=_user(self._text), metadata=(DynamicData.from_tagged(_Origin("base")),)
        )

    async def get_state(self) -> DynamicData:
        return DynamicData.from_tagged(_AnnotatingState())

    async def aclose(self) -> None:
        pass


def test_generic_candidate_with_the_marker_ends_the_rollout_keeping_its_metadata() -> None:
    async def run() -> None:
        steered, _ = _steered(_AnnotatingBase("bye</transcript>"), [_yaml([0, _SUPPRESS])])
        session = await steered.start_session()

        turn = await session.next_user_turn(None)
        assert isinstance(turn, EndRollout)
        assert find_metadata(turn.metadata, _Origin) == _Origin("base")
        assert _steering(turn).selected_text == "bye</transcript>"

    anyio.run(run)


def test_custom_text_is_rejected_when_disabled() -> None:
    async def run() -> None:
        base, _ = _base([["alpha"], ["beta"]])
        steered, pilot = _steered(
            base, [_yaml([0, 0], "mine"), _yaml([_SUPPRESS, 0])], allow_custom_text=False
        )
        session = await steered.start_session()

        turn = await session.next_user_turn(None)
        assert isinstance(turn, UserTurn) and turn.message == _user("beta")
        assert len(pilot.requests) == 2
        assert 'You do **not** have the "custom text" capability' in _text(
            pilot.requests[0].context[0]
        )

    anyio.run(run)


def test_first_turn_is_taken_from_the_base_when_not_steered() -> None:
    async def run() -> None:
        base = ScriptedUserSimulator(messages=(_user("one"), _user("two")))
        steered, pilot = _steered(base, [_yaml([0, _SUPPRESS])], steer_first_turn=False)
        session = await steered.start_session()

        assert await session.next_user_turn(None) == UserTurn(message=_user("one"))
        assert pilot.requests == []

        turn = await session.next_user_turn((_assistant("Go on."),))
        assert isinstance(turn, UserTurn) and turn.message == _user("two")
        assert _steering(turn).candidates == ("two", "two")
        assert len(pilot.requests) == 1

    anyio.run(run)


def test_malformed_pilot_responses_are_resampled_from_the_same_prompt() -> None:
    async def run() -> None:
        base, _ = _base([["alpha"], ["beta"]])
        steered, pilot = _steered(base, ["not: [valid yaml", _yaml([0]), _yaml([_SUPPRESS, 0])])
        session = await steered.start_session()

        turn = await session.next_user_turn(None)
        assert isinstance(turn, UserTurn) and turn.message == _user("beta")
        assert len(pilot.requests) == 3
        assert all(request == pilot.requests[0] for request in pilot.requests)
        assert _steering(turn).raw_response == _yaml([_SUPPRESS, 0])

    anyio.run(run)


def test_malformed_pilot_responses_exhaust_attempts() -> None:
    async def run() -> None:
        base, _ = _base([["alpha"], ["beta"]])
        steered, pilot = _steered(base, ["garbage"] * PILOT_RESPONSE_MAX_ATTEMPTS)
        session = await steered.start_session()

        with pytest.raises(ValueError):
            await session.next_user_turn(None)
        assert len(pilot.requests) == PILOT_RESPONSE_MAX_ATTEMPTS

    anyio.run(run)


class _ClosingSession:
    """Wraps a session, recording when it is closed."""

    def __init__(self, inner: SerializableUserSimulatorSession, closed: list[object]) -> None:
        self._inner = inner
        self._closed = closed

    async def next_user_turn(
        self,
        model_messages: tuple[ChatMessage, ...] | None,
        *,
        user_turn_override: UserMessage | None = None,
    ) -> UserTurn | EndRollout:
        return await self._inner.next_user_turn(
            model_messages, user_turn_override=user_turn_override
        )

    async def get_state(self) -> DynamicData:
        return await self._inner.get_state()

    async def aclose(self) -> None:
        await anyio.sleep(0)  # a real close awaits something (a client, a socket)
        self._closed.append(self)
        await self._inner.aclose()


class _BreakingBase:
    """A base whose ``breaks_on``-th restore raises or hangs, recording which sessions closed."""

    def __init__(self, *, breaks_on: int, hang: bool) -> None:
        self._inner = ScriptedUserSimulator(messages=(_user("one"),))
        self._breaks_on = breaks_on
        self._hang = hang
        self._restores = 0
        self.started: list[_ClosingSession] = []
        self.restored: list[_ClosingSession] = []
        self.closed: list[object] = []

    async def start_session(self) -> _ClosingSession:
        session = _ClosingSession(await self._inner.start_session(), self.closed)
        self.started.append(session)
        return session

    async def restore_session(self, state: DynamicData) -> _ClosingSession:
        self._restores += 1
        if self._restores == self._breaks_on:
            if self._hang:
                await anyio.sleep_forever()
            raise RuntimeError("restore failed")
        session = _ClosingSession(await self._inner.restore_session(state), self.closed)
        self.restored.append(session)
        return session


def test_failed_restore_closes_sessions_restored_before_it() -> None:
    async def run() -> None:
        base = _BreakingBase(breaks_on=2, hang=False)
        steered, _ = _steered(base, [])
        session = await steered.start_session()

        with pytest.raises(RuntimeError):
            await session.next_user_turn(None)
        assert base.closed == base.restored

    anyio.run(run)


def test_cancellation_during_restore_closes_sessions_restored_before_it() -> None:
    async def run() -> None:
        base = _BreakingBase(breaks_on=2, hang=True)
        steered, _ = _steered(base, [])
        session = await steered.start_session()

        with anyio.move_on_after(0.05) as scope:
            await session.next_user_turn(None)
        assert scope.cancelled_caught
        assert base.closed == base.restored

    anyio.run(run)


def test_cancellation_after_the_pilot_call_leaves_the_live_session_and_state_intact() -> None:
    async def run() -> None:
        # The custom-text session is the third restore; cancellation lands there.
        base = _BreakingBase(breaks_on=3, hang=True)
        steered, _ = _steered(base, [_yaml([0, 0], "mine")])
        session = await steered.start_session()
        before = await session.get_state()

        with anyio.move_on_after(0.05) as scope:
            await session.next_user_turn(None)
        assert scope.cancelled_caught
        assert base.closed == base.restored and len(base.restored) == 2
        assert base.started[0] not in base.closed
        assert await session.get_state() == before

    anyio.run(run)


def test_session_scope_releases_by_identity() -> None:
    @dataclasses.dataclass
    class EqualSession:
        closed: bool = False

        async def aclose(self) -> None:
            self.closed = True

    async def run() -> None:
        first, second = EqualSession(), EqualSession()
        assert first == second
        async with _SessionScope[EqualSession]() as scope:
            scope.add(first)
            scope.add(second)
            assert scope.release(second) is second
        assert first.closed and not second.closed

    anyio.run(run)


def test_state_round_trip_keeps_the_pilot_history() -> None:
    async def run() -> None:
        base, completions = _base([["alpha"], ["beta"], ["gamma"], ["delta"]])
        steered, pilot = _steered(base, [_yaml([_SUPPRESS, 0]), _yaml([0, _SUPPRESS])])
        session = await steered.start_session()
        await session.next_user_turn(None)
        state = await session.get_state()

        restored = await steered.restore_session(_json_roundtrip(state))
        assert await restored.get_state() == state

        turn = await restored.next_user_turn((_assistant("Hi!"),))
        assert isinstance(turn, UserTurn) and turn.message == _user("gamma")
        assert [message.role for message in pilot.requests[1].context] == [
            "system",
            "user",
            "assistant",
            "user",
        ]
        assert "<|im_start|>user\nbeta<|im_end|>" in completions.calls[2]["prompt"]

    anyio.run(run)
