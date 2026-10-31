"""Unit tests for the base-model user simulator, against a scripted completions client.

Prompt rendering is pinned here on small fixtures; the check that prompts and requests
match ABR's original simulator byte-for-byte lives in ABR's tests, next to its renderer.
"""

import contextlib
import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

import anyio
import httpx2
import openai
import pytest
from openai import AsyncOpenAI

from behaviors.implementations.context_policies._shared import SimulatedUserText
from behaviors.implementations.context_policies.base_model_prompt import (
    BASE_MODEL_PREFACE,
    CHATML_USER_START,
    ExampleTranscript,
    ExampleTranscriptMessage,
    ReferenceUser,
    SimulatedUserProfile,
    render_base_model_prompt,
    render_chatml_transcript,
)
from behaviors.implementations.context_policies.base_model_user_simulator import (
    BaseModelUserSimulator,
    BaseModelUserSimulatorConfig,
    EmptyCompletionError,
    RequestGuard,
)
from behaviors.implementations.context_policies.scripted import ScriptedUserSimulator
from behaviors.interfaces.context_policies import SerializableUserSimulator
from behaviors.types.content import ContentText
from behaviors.types.message import AssistantMessage, UserMessage
from behaviors.types.retry import RetryConfig
from behaviors.types.rollout import EndRollout, UserTurn
from fxtr.core.entity_data import thaw_fxtr_data
from fxtr.entity_defns.dataclass_bases import ConversionError
from fxtr.entity_defns.dataclass_entity import DataclassEntity
from fxtr.entity_defns.dynamic_data import DynamicData
from fxtr.storage.memory_storage import MemoryStorage


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
    """Serves one scripted response per ``create`` call, recording each call's kwargs.

    A response is either the delta texts to stream or an exception to raise.
    """

    def __init__(self, responses: list[list[str] | Exception]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _ScriptedStream:
        self.calls.append(kwargs)
        response = self._responses[len(self.calls) - 1]
        if isinstance(response, Exception):
            raise response
        return _ScriptedStream(response)


class _Client:
    def __init__(self, responses: list[list[str] | Exception]) -> None:
        self.completions = _ScriptedCompletions(responses)


_PROFILE = SimulatedUserProfile(
    user_id="u-target", transcript_id="t-target", user_bio="You are concise."
)
_REFERENCE = ReferenceUser(
    user_id="u-ref",
    user_bio="You are the reference.",
    example_transcript=ExampleTranscript(
        transcript_id="t-ref",
        messages=(
            ExampleTranscriptMessage(role="user", content="reference hello"),
            ExampleTranscriptMessage(role="assistant", content="reference reply"),
        ),
    ),
)
_NO_BACKOFF = RetryConfig(
    max_attempts=3, initial_backoff_seconds=0, max_backoff_seconds=0, jitter_seconds=0
)


def _text(
    source: Literal["sampled", "fixed", "override"], raw_text: str
) -> tuple[DynamicData, ...]:
    return (DynamicData.from_tagged(SimulatedUserText(source=source, raw_text=raw_text)),)


def _user(text: str) -> UserMessage:
    return UserMessage(content=(ContentText(text=text),))


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=(ContentText(text=text),))


def _simulator(
    responses: list[list[str] | Exception],
    *,
    profile: SimulatedUserProfile = _PROFILE,
    retry: RetryConfig | None = None,
    request_guard: RequestGuard | None = None,
) -> tuple[BaseModelUserSimulator, _ScriptedCompletions]:
    client = _Client(responses)
    config = BaseModelUserSimulatorConfig(
        profile=profile,
        model="base-model",
        references=(_REFERENCE,),
        max_tokens=64,
        temperature=0.9,
    )
    simulator = BaseModelUserSimulator(
        config, client=cast(AsyncOpenAI, client), retry=retry, request_guard=request_guard
    )
    return simulator, client.completions


def test_conforms_to_serializable_user_simulator() -> None:
    simulator, _ = _simulator([])
    assert isinstance(simulator, SerializableUserSimulator)


def test_render_chatml_transcript_appends_user_prefix() -> None:
    rendered = render_chatml_transcript((_user("hi"), _assistant("hello")))
    assert "<|im_start|>user\nhi<|im_end|>" in rendered
    assert "<|im_start|>assistant\nhello<|im_end|>" in rendered
    assert rendered.endswith(CHATML_USER_START)


def test_render_base_model_prompt_contains_profile_and_transcript() -> None:
    profile = dataclasses.replace(_PROFILE, initial_user_message="hey")
    prompt = render_base_model_prompt(
        profile=profile, transcript=(_assistant("How can I help?"),), use_preface=False
    )
    assert prompt.startswith('<user_bio user_id="u-target">\nYou are concise.\n</user_bio>\n')
    assert '<transcript transcript_id="t-target" user_id="u-target">' in prompt
    # Only the bio is rendered, never the rest of the profile.
    assert "hey" not in prompt
    assert "<|im_start|>assistant\nHow can I help?<|im_end|>" in prompt
    assert prompt.endswith(CHATML_USER_START)


def test_render_base_model_prompt_includes_preface_and_reference_example() -> None:
    prompt = render_base_model_prompt(profile=_PROFILE, references=(_REFERENCE,))
    assert prompt.startswith(BASE_MODEL_PREFACE)
    assert '<user_bio user_id="u-ref">\nYou are the reference.\n</user_bio>' in prompt
    assert '<transcript transcript_id="t-ref" user_id="u-ref">' in prompt
    assert "<|im_start|>user\nreference hello<|im_end|>" in prompt
    assert "<|im_start|>assistant\nreference reply<|im_end|>" in prompt
    assert prompt.index("u-ref") < prompt.index("u-target")


def test_sampled_turn_is_one_streamed_completion_of_the_rendered_prompt() -> None:
    async def run() -> None:
        simulator, completions = _simulator([["he", "llo"], ["again"]])
        session = await simulator.start_session()

        turn = await session.next_user_turn(None)
        assert turn == UserTurn(message=_user("hello"), metadata=_text("sampled", "hello"))
        [call] = completions.calls
        assert call == {
            "model": "base-model",
            "prompt": render_base_model_prompt(profile=_PROFILE, references=(_REFERENCE,)),
            "n": 1,
            "stream": True,
            "max_tokens": 64,
            "temperature": 0.9,
            "stop": ["<|im_end|>", "<|im_start|>assistant\n"],
        }

        reply = _assistant("Hi!")
        await session.next_user_turn((reply,))
        assert completions.calls[1]["prompt"] == render_base_model_prompt(
            profile=_PROFILE, transcript=(_user("hello"), reply), references=(_REFERENCE,)
        )

    anyio.run(run)


def test_initial_user_message_is_sent_without_a_request() -> None:
    async def run() -> None:
        profile = dataclasses.replace(_PROFILE, initial_user_message="hey there")
        simulator, completions = _simulator([["second"]], profile=profile)
        session = await simulator.start_session()

        assert await session.next_user_turn(None) == UserTurn(
            message=_user("hey there"),
            metadata=_text("fixed", "hey there"),
        )
        assert completions.calls == []

        turn = await session.next_user_turn((_assistant("hi"),))
        assert isinstance(turn, UserTurn) and turn.message == _user("second")
        assert "<|im_start|>user\nhey there<|im_end|>" in completions.calls[0]["prompt"]

    anyio.run(run)


def test_end_marker_alone_ends_the_rollout() -> None:
    async def run() -> None:
        simulator, _ = _simulator([["</transcript>", " and then some continuation"]])
        session = await simulator.start_session()
        assert await session.next_user_turn(None) == EndRollout(
            metadata=_text("sampled", "</transcript>")
        )

    anyio.run(run)


def test_end_marker_after_text_ends_the_rollout_immediately() -> None:
    async def run() -> None:
        simulator, completions = _simulator([["bye now</transcript>", "\nmore text"]])
        session = await simulator.start_session()

        assert await session.next_user_turn(None) == EndRollout(
            metadata=_text("sampled", "bye now</transcript>")
        )
        assert len(completions.calls) == 1

    anyio.run(run)


def test_empty_completions_and_api_errors_retry() -> None:
    async def run() -> None:
        connection_error = openai.APIConnectionError(request=httpx2.Request("POST", "http://base"))
        simulator, completions = _simulator([[""], connection_error, ["ok"]], retry=_NO_BACKOFF)
        session = await simulator.start_session()

        turn = await session.next_user_turn(None)
        assert isinstance(turn, UserTurn) and turn.message == _user("ok")
        assert len(completions.calls) == 3

    anyio.run(run)


def test_raises_once_the_retry_budget_is_spent() -> None:
    async def run() -> None:
        simulator, completions = _simulator([[""], [""], [""]], retry=_NO_BACKOFF)
        session = await simulator.start_session()
        with pytest.raises(EmptyCompletionError):
            await session.next_user_turn(None)
        assert len(completions.calls) == 3

    anyio.run(run)


def test_request_guard_is_held_around_each_request() -> None:
    async def run() -> None:
        events: list[str] = []

        @contextlib.asynccontextmanager
        async def guard() -> AsyncIterator[None]:
            events.append("enter")
            yield
            events.append("exit")

        simulator, _ = _simulator([[""], ["ok"]], retry=_NO_BACKOFF, request_guard=guard)
        session = await simulator.start_session()
        await session.next_user_turn(None)
        assert events == ["enter", "exit", "enter", "exit"]

    anyio.run(run)


def test_restored_sessions_continue_independently() -> None:
    async def run() -> None:
        simulator, completions = _simulator([["first"], ["a"], ["b"]])
        session = await simulator.start_session()
        await session.next_user_turn(None)
        state = await session.get_state()

        forks = [await simulator.restore_session(state) for _ in range(2)]
        assert await forks[0].get_state() == state

        reply = (_assistant("and?"),)
        turns = [await fork.next_user_turn(reply) for fork in forks]
        assert [turn.message for turn in turns if isinstance(turn, UserTurn)] == [
            _user("a"),
            _user("b"),
        ]
        assert completions.calls[1]["prompt"] == completions.calls[2]["prompt"]
        assert await session.get_state() == state

    anyio.run(run)


def test_override_is_recorded_as_the_users_turn() -> None:
    async def run() -> None:
        simulator, completions = _simulator([["next"]])
        session = await simulator.start_session()
        override = _user("I'd rather talk about cats")

        turn = await session.next_user_turn(None, user_turn_override=override)
        assert turn == UserTurn(
            message=override,
            metadata=_text("override", "I'd rather talk about cats"),
        )
        assert completions.calls == []

        await session.next_user_turn((_assistant("Cats it is."),))
        assert (
            "<|im_start|>user\nI'd rather talk about cats<|im_end|>\n"
            "<|im_start|>assistant\nCats it is.<|im_end|>"
        ) in completions.calls[0]["prompt"]

    anyio.run(run)


def test_restore_from_json_state_and_reject_corrupt_states() -> None:
    async def run() -> None:
        simulator, completions = _simulator([["first"], ["second"]])
        session = await simulator.start_session()
        await session.next_user_turn(None)
        state = await session.get_state()
        persisted = DynamicData.from_data(json.loads(json.dumps(thaw_fxtr_data(state.data))))
        restored = await simulator.restore_session(persisted)
        assert await restored.get_state() == state
        await restored.next_user_turn((_assistant("and?"),))
        assert "<|im_start|>user\nfirst<|im_end|>" in completions.calls[1]["prompt"]

        payload: Any = thaw_fxtr_data(state.data)
        for corrupt in (
            {key: value for key, value in payload.items() if key != "messages"},
            {**payload, "extra": 1},
            {**payload, "messages": "not a tuple"},
        ):
            with pytest.raises(ConversionError):
                await simulator.restore_session(DynamicData.from_data(corrupt))
        scripted = await ScriptedUserSimulator(messages=()).start_session()
        with pytest.raises(ConversionError):
            await simulator.restore_session(await scripted.get_state())

    anyio.run(run)


@dataclasses.dataclass(frozen=True)
class _StoredRecords(DataclassEntity, fxtr_type="test.behaviors.BaseModelUserSimulatorRecords.v1"):
    config: BaseModelUserSimulatorConfig
    turn: UserTurn


@pytest.mark.anyio
async def test_config_and_turn_metadata_storage_roundtrip() -> None:
    original = _StoredRecords(
        config=BaseModelUserSimulatorConfig(
            profile=dataclasses.replace(_PROFILE, initial_user_message="hey"),
            model="base-model",
            references=(_REFERENCE,),
            temperature=0.9,
        ),
        turn=UserTurn(message=_user("hi"), metadata=_text("sampled", " hi")),
    )
    store = MemoryStorage()
    ref = await store.store(original)
    restored = await store.load(ref)
    assert restored == original
    assert (await store.store(restored)).entity_id == ref.entity_id
