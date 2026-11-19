"""ChatRolloutPreview: a chat rollout's events folded into a partial rollout."""

import dataclasses
import functools
from typing import cast

import anyio

from behaviors.implementations.context_policies import (
    ScriptedUserSimulator,
    context_policy_from_user_simulator,
)
from behaviors.implementations.dialects.anthropic_messages import AnthropicSignature
from behaviors.implementations.models import chat_session_model_from_api
from behaviors.rollouts.chat import (
    ContextTurnEvent,
    ModelStreamEvent,
    ModelTurnEvent,
    RolloutEndEvent,
    run_chat_rollout,
)
from behaviors.rollouts.chat_preview import ChatRolloutPreview
from behaviors.types.content import ContentReasoning, ContentText, ContentToolUse
from behaviors.types.failure import ModelCallFailure
from behaviors.types.message import AssistantMessage, UserMessage
from behaviors.types.response import ModelResponse, SamplingParams, StopEndTurn, Usage
from behaviors.types.rollout import (
    ChatRollout,
    ContextTurn,
    EndRollout,
    ModelTurn,
    RolloutCompleted,
    RolloutFailed,
)
from behaviors.types.streaming import (
    StreamEvent,
    StreamEventCallback,
    StreamReasoningDelta,
    StreamReasoningStart,
    StreamReasoningState,
    StreamRestart,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
)
from fxtr.entity_defns.converter import DEFAULT_CONVERTER
from fxtr.entity_defns.dynamic_data import DynamicData
from fxtr.entity_defns.partial import Partial

CONTEXT = ContextTurn(
    messages=(UserMessage(content=(ContentText(text="hi"),)),), available_tools=()
)
REPLY = ModelTurn(messages=(AssistantMessage(content=(ContentText(text="hello"),)),))


def in_flight(*content: object) -> Partial[ModelTurn]:
    return Partial(ModelTurn, messages=(Partial(AssistantMessage, content=content),))


def feed(preview: ChatRolloutPreview, *deltas: StreamEvent) -> None:
    async def go() -> None:
        for delta in deltas:
            await preview(ModelStreamEvent(event=delta))

    anyio.run(go)


def test_turns_and_deltas_fold_into_the_rollout_so_far() -> None:
    published: list[Partial[ChatRollout]] = []
    preview = ChatRolloutPreview(lambda build: published.append(build()))
    anyio.run(preview, ContextTurnEvent(turn=CONTEXT))
    assert preview.value == Partial(ChatRollout, context_turns=(CONTEXT,), model_turns=())

    signature = DynamicData.from_tagged(AnthropicSignature(signature="sig"))
    feed(
        preview,
        StreamReasoningStart(form="summary"),
        StreamReasoningDelta(text="think"),
        StreamReasoningDelta(text="ing"),
        StreamReasoningState(state=signature),
        StreamReasoningDelta(text="raw"),
        StreamTextDelta(text="Hel"),
        StreamTextDelta(text="lo"),
        StreamToolUseStart(index=0, id="call_1", name="search"),
        StreamToolUseInputDelta(index=0, partial_json='{"q": "ca'),
    )
    tool = Partial(ContentToolUse, id="call_1", name="search")
    expected = in_flight(
        ContentReasoning(text="thinking", form="summary", reasoning_state=signature),
        ContentReasoning(text="raw", form="full"),
        ContentText(text="Hello"),
        tool,
    )
    assert preview.value.model_turns == (expected,)
    assert preview.value.status is None and preview.value.end is None

    feed(preview, StreamToolUseInputDelta(index=0, partial_json='ts"}'))
    (turn,) = preview.value.model_turns
    (message,) = turn.messages
    assert message.content[-1] == tool.replace(input={"q": "cats"}, parse_error=None)

    anyio.run(preview, ModelTurnEvent(turn=REPLY))
    assert preview.value == Partial(ChatRollout, context_turns=(CONTEXT,), model_turns=(REPLY,))
    # One publication per event, each the rollout as of that event.
    assert len(published) == 1 + 9 + 1 + 1
    assert published[-1] == preview.value


def test_a_restart_discards_the_turn_in_flight() -> None:
    published: list[Partial[ChatRollout]] = []
    preview = ChatRolloutPreview(lambda build: published.append(build()))
    anyio.run(preview, ContextTurnEvent(turn=CONTEXT))
    feed(preview, StreamTextDelta(text="lost"), StreamRestart())
    # No turn is in flight until the retried reply begins: the model has not begun it.
    assert preview.value.model_turns == () and published[-1].model_turns == ()
    feed(preview, StreamTextDelta(text="kept"))
    assert preview.value.model_turns == (in_flight(ContentText(text="kept")),)


def test_a_restart_with_no_turn_in_flight_changes_nothing() -> None:
    published: list[Partial[ChatRollout]] = []
    preview = ChatRolloutPreview(lambda build: published.append(build()))
    anyio.run(preview, ContextTurnEvent(turn=CONTEXT))
    feed(preview, StreamRestart())
    assert preview.value.model_turns == () and len(published) == 1


def test_the_end_of_the_rollout_sets_how_it_ended() -> None:
    published: list[Partial[ChatRollout]] = []
    preview = ChatRolloutPreview(lambda build: published.append(build()))
    anyio.run(preview, ContextTurnEvent(turn=CONTEXT))
    anyio.run(preview, ModelTurnEvent(turn=REPLY))
    assert preview.value.status is None and preview.value.end is None
    end = EndRollout()
    anyio.run(preview, RolloutEndEvent(status=RolloutCompleted(), end=end))
    assert published[-1].status == RolloutCompleted() and published[-1].end == end
    assert published[-1].model_turns == (REPLY,)


def test_a_rollout_ending_on_a_failure_discards_the_turn_in_flight() -> None:
    failure = ModelCallFailure(
        category="context_length", description="too long", provider_http_status=400
    )
    preview = ChatRolloutPreview()
    anyio.run(preview, ContextTurnEvent(turn=CONTEXT))
    feed(preview, StreamTextDelta(text="lost"))
    anyio.run(preview, RolloutEndEvent(status=RolloutFailed(failure=failure), end=None))
    # As the returned rollout: the last context turn has no reply.
    assert preview.value.model_turns == ()
    assert preview.value.status == RolloutFailed(failure=failure) and preview.value.end is None


@dataclasses.dataclass(frozen=True)
class Keepalive:
    """A stream event carrying nothing to assemble, as a provider's keepalive does."""


def test_a_delta_carrying_nothing_neither_starts_a_turn_nor_publishes() -> None:
    published: list[Partial[ChatRollout]] = []
    preview = ChatRolloutPreview(lambda build: published.append(build()))
    anyio.run(preview, ContextTurnEvent(turn=CONTEXT))
    keepalive = cast(StreamEvent, Keepalive())
    # Before the model's first output: the model has not begun its reply, and the preview says so.
    feed(preview, keepalive)
    assert preview.value.model_turns == ()
    assert len(published) == 1
    # Mid-reply: the text is untouched, and nothing new is published.
    feed(preview, StreamTextDelta(text="Hel"), keepalive, StreamTextDelta(text="lo"))
    assert preview.value.model_turns == (in_flight(ContentText(text="Hello")),)
    assert len(published) == 3


def test_previews_serialize_round_trip() -> None:
    preview = ChatRolloutPreview()
    anyio.run(preview, ContextTurnEvent(turn=CONTEXT))
    feed(preview, StreamTextDelta(text="Hel"), StreamToolUseStart(index=0, id="c", name="f"))
    value = preview.value
    data = DEFAULT_CONVERTER.unstructure_tagged(value)
    assert DEFAULT_CONVERTER.structure(data, Partial[ChatRollout]) == value


class StreamingAPI:
    """Streams the response's text in two deltas, then returns it."""

    async def generate(self, request: object) -> ModelResponse:
        return ModelResponse(
            id="msg_1",
            output=(AssistantMessage(content=(ContentText(text="hello"),)),),
            stop_reason=StopEndTurn(),
            usage=Usage(
                input_tokens=1,
                cache_read_input_tokens=None,
                cache_write_input_tokens=None,
                output_tokens=1,
                reasoning_output_tokens=None,
            ),
        )

    async def generate_with_events(
        self, request: object, callback: StreamEventCallback
    ) -> ModelResponse:
        await callback(StreamTextDelta(text="hel"))
        await callback(StreamTextDelta(text="lo"))
        return await self.generate(request)


def test_a_live_rollout_publishes_each_step_and_ends_on_its_turns() -> None:
    published: list[Partial[ChatRollout]] = []
    model = chat_session_model_from_api(StreamingAPI(), SamplingParams())
    policy = context_policy_from_user_simulator(
        ScriptedUserSimulator(messages=(UserMessage(content=(ContentText(text="hi"),)),)),
        initialization=None,
    )
    rollout = anyio.run(
        functools.partial(
            run_chat_rollout,
            model,
            policy,
            event_callback=ChatRolloutPreview(lambda build: published.append(build())),
        )
    )
    assert [p.model_turns for p in published[:3]] == [
        (),
        (in_flight(ContentText(text="hel")),),
        (in_flight(ContentText(text="hello")),),
    ]
    assert published[-1].context_turns == rollout.context_turns
    assert published[-1].model_turns == rollout.model_turns
    # The last preview says how the rollout ended, as the rollout itself does.
    assert published[-1].status == rollout.status and published[-1].end == rollout.end
    assert all(p.status is None for p in published[:-1])
