"""Watching a chat rollout live: its events folded into a partial rollout.

:class:`ChatRolloutPreview` is a ``run_chat_rollout`` event callback that keeps a
``Partial[ChatRollout]`` of the rollout so far (see :mod:`fxtr.entity_defns.partial`) and, after
each event that changes it, tells a publish function, typically a step's ``ctx.preview``. It
publishes a function building the rollout so far rather than the rollout itself, so a step's
preview is built only when the host writes it, a few times a second, not on every streamed
token::

    rollout = await run_chat_rollout(
        model, policy, event_callback=ChatRolloutPreview(context.preview)
    )

The partial rollout holds every finished context and model turn, and while the model is
generating (with a streaming session) a partial last model turn: one partial assistant message
whose content is the stream so far. A reasoning start opens a reasoning block of its form; a
reasoning delta extends the last block if it is reasoning without state yet, or else opens a
``full`` one; a reasoning state completes the last such block, or else is a ``summary`` block of
its own; a text delta extends the last block if it is text, or starts one; a tool call is a
partial ``ContentToolUse`` whose ``input`` is set once its streamed JSON parses as an object.
A ``StreamRestart`` discards the turn in flight, leaving none until the retried reply's first
delta, so the rollout reads as waiting for the model again; the finished turn replaces a turn in
flight when it arrives. A delta carrying nothing to assemble (a ``StreamKeepalive``, or one this
does not know) changes nothing: it neither starts a turn in flight nor publishes, so a model that
has not begun its reply shows as not having begun.
``status`` and ``end`` are ``None`` until the rollout ends (``RolloutEndEvent``), then what the
returned rollout carries, so a reader can tell a rollout that has ended from one waiting on its
next turn; an end discards a turn in flight too, as a rollout that ends on a recorded failure has
no reply for its last context turn. ``initialization`` stays ``None``, since the rollout reports
it only on its result.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable

from behaviors.rollouts.chat import (
    ChatRolloutEvent,
    ContextTurnEvent,
    ModelStreamEvent,
    ModelTurnEvent,
    RolloutEndEvent,
)
from behaviors.types.content import ContentReasoning, ContentText, ContentToolUse
from behaviors.types.message import AssistantMessage
from behaviors.types.rollout import ChatRollout, ContextTurn, EndRollout, ModelTurn, RolloutStatus
from behaviors.types.streaming import (
    StreamEvent,
    StreamReasoningDelta,
    StreamReasoningStart,
    StreamReasoningState,
    StreamRestart,
    StreamTextDelta,
    StreamToolUseInputDelta,
    StreamToolUseStart,
)
from fxtr.entity_defns.partial import Partial


@dataclasses.dataclass
class _ToolCall:
    """A tool call being streamed: its start, and its JSON input so far."""

    id: str
    name: str
    json: str = ""

    def block(self) -> Partial[ContentToolUse]:
        try:
            parsed = json.loads(self.json) if self.json else None
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            return Partial(ContentToolUse, id=self.id, name=self.name)
        return Partial(ContentToolUse, id=self.id, name=self.name, input=parsed, parse_error=None)


type _Block = ContentText | ContentReasoning | _ToolCall


class ChatRolloutPreview:
    """A ``run_chat_rollout`` event callback keeping the rollout so far (see the module docstring).

    Args:
        publish: Called after every event that changes the rollout, with a function returning
            the rollout so far (:attr:`value`) whenever it is called, e.g. a step's
            ``ctx.preview``; ``None`` to only keep it, for reading :attr:`value`.
    """

    def __init__(
        self, publish: Callable[[Callable[[], Partial[ChatRollout]]], object] | None = None
    ) -> None:
        self._publish = publish
        self._context_turns: list[ContextTurn] = []
        self._model_turns: list[ModelTurn] = []
        self._streaming: list[_Block] | None = None
        self._tool_calls: dict[int, _ToolCall] = {}
        self._status: RolloutStatus | None = None
        self._end: EndRollout | None = None

    @property
    def value(self) -> Partial[ChatRollout]:
        """The rollout so far."""
        model_turns: tuple[ModelTurn | Partial[ModelTurn], ...] = tuple(self._model_turns)
        if self._streaming is not None:
            model_turns += (self._turn_in_flight(self._streaming),)
        return Partial(
            ChatRollout,
            context_turns=tuple(self._context_turns),
            model_turns=model_turns,
            status=self._status,
            end=self._end,
        )

    async def __call__(self, event: ChatRolloutEvent) -> None:
        match event:
            case ContextTurnEvent(turn=turn):
                self._context_turns.append(turn)
                self._end_stream()
            case ModelTurnEvent(turn=turn):
                self._model_turns.append(turn)
                self._end_stream()
            case ModelStreamEvent(event=delta):
                if not self._apply(delta):
                    return
            case RolloutEndEvent(status=status, end=end):
                self._status = status
                self._end = end
                self._end_stream()
        if self._publish is not None:
            self._publish(self._current)

    def _current(self) -> Partial[ChatRollout]:
        return self.value

    def _end_stream(self) -> None:
        self._streaming = None
        self._tool_calls = {}

    def _apply(self, delta: StreamEvent) -> bool:
        """Fold one delta into the turn in flight, starting the turn if it is the first. Returns
        whether the rollout so far changed: a delta carrying nothing to assemble (one this does not
        know, such as a keepalive) changes nothing, so it neither starts a turn nor is published.
        A restart discards the turn in flight, if there is one."""
        if isinstance(delta, StreamRestart):
            discarded = self._streaming is not None
            self._end_stream()
            return discarded
        blocks = self._streaming if self._streaming is not None else []
        last = blocks[-1] if blocks else None
        match delta:
            case StreamTextDelta(text=text):
                if isinstance(last, ContentText):
                    blocks[-1] = ContentText(text=last.text + text)
                else:
                    blocks.append(ContentText(text=text))
            case StreamReasoningStart(form=form):
                blocks.append(ContentReasoning(text="", form=form))
            case StreamReasoningDelta(text=text):
                if isinstance(last, ContentReasoning) and last.reasoning_state is None:
                    blocks[-1] = dataclasses.replace(last, text=last.text + text)
                else:
                    blocks.append(ContentReasoning(text=text, form="full"))
            case StreamReasoningState(state=state):
                if isinstance(last, ContentReasoning) and last.reasoning_state is None:
                    blocks[-1] = dataclasses.replace(last, reasoning_state=state)
                else:
                    blocks.append(ContentReasoning(text="", form="summary", reasoning_state=state))
            case StreamToolUseStart(index=index, id=call_id, name=name):
                call = _ToolCall(id=call_id, name=name)
                self._tool_calls[index] = call
                blocks.append(call)
            case StreamToolUseInputDelta(index=index, partial_json=fragment):
                call = self._tool_calls.get(index)
                if call is None:
                    return False
                call.json += fragment
            case _:
                return False
        self._streaming = blocks
        return True

    @staticmethod
    def _turn_in_flight(blocks: list[_Block]) -> Partial[ModelTurn]:
        content = tuple(
            block.block() if isinstance(block, _ToolCall) else block for block in blocks
        )
        return Partial(ModelTurn, messages=(Partial(AssistantMessage, content=content),))
