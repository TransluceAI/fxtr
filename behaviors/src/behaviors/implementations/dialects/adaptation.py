"""Building blocks for `ModelSideCodec.adapt_request`: changing a vocabulary request so a dialect
can carry it, recording one `RequestLoss` per change.

A codec's `adapt_request` is a short sequence of these steps over an `AdaptedRequestBuilder`,
which holds the request as it is being changed and the losses recorded so far. Every step changes
the request only where its dialect cannot carry it, and records what it changed at the point it
changes it, so the losses are what was done rather than a guess about it. The encoder then renders
the adapted request exactly; an input it cannot render is a `CodecError`, not something it drops.
"""

import dataclasses
import re
from collections.abc import Callable, Sequence
from typing import Any

from behaviors.implementations.dialects._common import (
    ReasoningStateFilter,
    wire_tool_names,
    without_foreign_reasoning_state,
)
from behaviors.interfaces.dialect_codecs import AdaptedRequest
from behaviors.types.adaptation import RequestLoss
from behaviors.types.cache_control import CacheControl
from behaviors.types.content import (
    CacheableContentText,
    CacheableContentToolUse,
    Content,
    ContentReasoning,
    ContentText,
    ContentToolUse,
)
from behaviors.types.dialect import Dialect
from behaviors.types.message import (
    AssistantMessage,
    CacheableToolMessage,
    ChatMessage,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from behaviors.types.response import ModelRequest, ReasoningParams
from behaviors.types.tool import CacheableToolParam, ToolParam


class AdaptedRequestBuilder:
    """A request being adapted, and the losses its changes recorded.

    `note` records a change described once (a setting replaced); `count` records one occurrence of
    a change that can happen many times (a reasoning block dropped), folded into one loss per
    code with the counts in its detail. `result` is the `AdaptedRequest`.
    """

    def __init__(self, request: ModelRequest) -> None:
        self.request = request
        self._notes: dict[str, list[str]] = {}
        self._counts: dict[str, dict[str, int]] = {}
        self._semantic: dict[str, bool] = {}

    def _key(self, code: str, semantic: bool) -> None:
        known = self._semantic.setdefault(code, semantic)
        if known != semantic:
            raise ValueError(f"loss {code!r} recorded as both semantic and not")

    def note(self, code: str, detail: str, *, semantic: bool = True) -> None:
        self._key(code, semantic)
        self._notes.setdefault(code, []).append(detail)
        self._counts.setdefault(code, {})

    def count(self, code: str, noun: str, *, semantic: bool = True, n: int = 1) -> None:
        self._key(code, semantic)
        self._notes.setdefault(code, [])
        nouns = self._counts.setdefault(code, {})
        nouns[noun] = nouns.get(noun, 0) + n

    def result(self) -> AdaptedRequest:
        losses: list[RequestLoss] = []
        for code, notes in self._notes.items():
            counted = [f"{n} {noun}" for noun, n in self._counts.get(code, {}).items()]
            losses.append(RequestLoss(code, "; ".join([*notes, *counted]), self._semantic[code]))
        return AdaptedRequest(request=self.request, losses=tuple(losses))


# --- context -------------------------------------------------------------------------------------


def drop_assistant_blocks(
    adaptation: AdaptedRequestBuilder,
    which: Callable[[Content], bool],
    code: str,
    noun: str,
    *,
    semantic: bool = True,
) -> None:
    """Remove the assistant-message blocks `which` selects, counting each as `noun`."""
    dropped = 0
    context: list[ChatMessage] = []
    for message in adaptation.request.context:
        if isinstance(message, AssistantMessage):
            kept = tuple(block for block in message.content if not which(block))
            if len(kept) != len(message.content):
                dropped += len(message.content) - len(kept)
                message = dataclasses.replace(message, content=kept)
        context.append(message)
    if dropped:
        adaptation.count(code, noun, semantic=semantic, n=dropped)
        adaptation.request = dataclasses.replace(adaptation.request, context=tuple(context))


def drop_foreign_reasoning(
    adaptation: AdaptedRequestBuilder, accepts: ReasoningStateFilter
) -> None:
    """Remove reasoning whose state `accepts` (the codec's `accepts_reasoning_state` for the model
    called) rejects, text and all (`without_foreign_reasoning_state`): a provider verifies what it
    issued, so the model reasons afresh on that turn instead."""
    request, types = without_foreign_reasoning_state(adaptation.request, accepts)
    if types:
        adaptation.count(
            "foreign_reasoning",
            f"reasoning block(s) with another provider's state ({', '.join(sorted(set(types)))})",
            n=len(types),
        )
        adaptation.request = request


def drop_stateless_reasoning(adaptation: AdaptedRequestBuilder, what: str) -> None:
    """Remove reasoning that carries no state, for a dialect that takes reasoning back only with
    its own provider's state; `what` names that state. Each form is its own loss: the model's own
    reasoning text (`full`) is `literal_reasoning`, a summary `unsigned_reasoning`, a redaction
    `redacted_reasoning`."""
    for form, code, noun in (
        ("full", "literal_reasoning", "reasoning text block(s)"),
        ("summary", "unsigned_reasoning", "reasoning summary block(s)"),
        ("redacted", "redacted_reasoning", "redacted reasoning block(s)"),
    ):
        drop_assistant_blocks(
            adaptation,
            lambda block, form=form: (
                isinstance(block, ContentReasoning)
                and block.reasoning_state is None
                and block.form == form
            ),
            code,
            f"{noun} with no {what}",
        )


def tool_results_first(adaptation: AdaptedRequestBuilder) -> None:
    """Put each user turn's tool results before its other messages, for a dialect that requires
    a turn's results first (Anthropic, chat completions). A user turn is the run of user and tool
    messages between two other messages; its tool results keep their own order, and so does the
    rest of the turn."""
    context: list[ChatMessage] = []
    turn: list[ChatMessage] = []
    moved = 0

    def flush() -> None:
        nonlocal moved
        results = [m for m in turn if isinstance(m, ToolMessage)]
        others = [m for m in turn if not isinstance(m, ToolMessage)]
        ordered = [*results, *others]
        if ordered != turn:
            moved += 1
        context.extend(ordered)
        turn.clear()

    for message in adaptation.request.context:
        if isinstance(message, UserMessage | ToolMessage):
            turn.append(message)
            continue
        flush()
        context.append(message)
    flush()
    if moved:
        adaptation.count(
            "tool_result_order", "user turn(s) with tool results after other content", n=moved
        )
        adaptation.request = dataclasses.replace(adaptation.request, context=tuple(context))


def drop_foreign_extras(adaptation: AdaptedRequestBuilder, dialect: Dialect) -> None:
    """Remove dialect extras written in another dialect: only a codec of that dialect could apply
    them (see `behaviors.types.dialect.DialectExtras`)."""
    extras = adaptation.request.extras
    if extras is None or extras.dialect == dialect:
        return
    keys = [
        *extras.body.keys(),
        *(f"header:{name}" for name, _ in extras.headers),
        *(f"query:{name}" for name, _ in extras.query),
    ]
    adaptation.note("extras", f"{extras.dialect} extras not sent: {', '.join(keys)}")
    adaptation.request = dataclasses.replace(adaptation.request, extras=None)


def clear_tool_error_flags(adaptation: AdaptedRequestBuilder) -> None:
    """Clear `ToolMessage.is_error`, for a dialect with no error flag on a tool result: a failure
    reaches the model only as the result's text."""
    cleared = 0
    context: list[ChatMessage] = []
    for message in adaptation.request.context:
        if isinstance(message, ToolMessage) and message.is_error is not None:
            cleared += 1
            message = dataclasses.replace(message, is_error=None)
        context.append(message)
    if cleared:
        adaptation.count("tool_error_flag", "tool result error flag(s)", n=cleared)
        adaptation.request = dataclasses.replace(adaptation.request, context=tuple(context))


def later_system_messages_as_user(adaptation: AdaptedRequestBuilder) -> None:
    """Send each `SystemMessage` after the conversation began as a `UserMessage`, for a dialect
    with no system role inside the conversation: its instructions reach the model as the user's
    words."""
    moved = 0
    leading = True
    context: list[ChatMessage] = []
    for message in adaptation.request.context:
        if isinstance(message, SystemMessage):
            if not leading:
                moved += 1
                message = UserMessage(content=message.content)
        else:
            leading = False
        context.append(message)
    if moved:
        adaptation.count("system_role", "system message(s) after the conversation began", n=moved)
        adaptation.request = dataclasses.replace(adaptation.request, context=tuple(context))


def note_flattened_tools(adaptation: AdaptedRequestBuilder) -> None:
    """Record what a dialect without tool namespaces or freeform tools shows the model
    differently, for its encoder renders it so (`_common.wire_tool_names`): a grouped tool whose
    name another declaration shares goes by `group__name`, and a freeform tool with a grammar is
    one free string field, the grammar unenforced. The request itself is unchanged; the loss is
    what the model sees."""
    wire = wire_tool_names(adaptation.request.tools)
    renamed = [
        f"{tool.name} as {wire[(tool.namespace, tool.name)]}"
        for tool in adaptation.request.tools
        if wire[(tool.namespace, tool.name)] != tool.name
    ]
    if renamed:
        adaptation.note(
            "tool_name", f"tools whose name another group shares renamed: {', '.join(renamed)}"
        )
    grammars = [
        tool.name
        for tool in adaptation.request.tools
        if tool.freeform_format is not None and tool.freeform_format.get("type") != "text"
    ]
    if grammars:
        adaptation.note(
            "freeform_format",
            f"input grammar not enforced, the input one free string: {', '.join(grammars)}",
        )


# --- sampling ------------------------------------------------------------------------------------


def replace_reasoning(
    adaptation: AdaptedRequestBuilder, reasoning: ReasoningParams, code: str, detail: str
) -> None:
    """Send `reasoning` in place of the request's reasoning settings: the nearest the dialect can
    express. The model reasons differently than asked, a semantic loss."""
    sampling = dataclasses.replace(adaptation.request.sampling, reasoning=reasoning)
    adaptation.request = dataclasses.replace(adaptation.request, sampling=sampling)
    adaptation.note(code, detail)


def drop_stop_sequences(adaptation: AdaptedRequestBuilder) -> None:
    """Remove the stop sequences, for a dialect with no parameter for them."""
    sampling = adaptation.request.sampling
    if not sampling.stop_sequences:
        return
    adaptation.note("stop_sequences", f"{len(sampling.stop_sequences)} stop sequence(s) not sent")
    adaptation.request = dataclasses.replace(
        adaptation.request, sampling=dataclasses.replace(sampling, stop_sequences=())
    )


def drop_speed(adaptation: AdaptedRequestBuilder) -> None:
    """Remove the speed tier, for a dialect with no parameter for it: the request is served at
    the standard tier, which changes latency and cost only."""
    sampling = adaptation.request.sampling
    if sampling.speed is None:
        return
    adaptation.note("speed", f"speed tier {sampling.speed} not sent", semantic=False)
    adaptation.request = dataclasses.replace(
        adaptation.request, sampling=dataclasses.replace(sampling, speed=None)
    )


# --- cache breakpoints ---------------------------------------------------------------------------


def _plain[T](value: T, base: type[T]) -> T:
    """`value` as its non-cacheable base class (the same fields, without `cache_control`)."""
    return base(**{f.name: getattr(value, f.name) for f in dataclasses.fields(base)})  # type: ignore[arg-type]


def breakpoint_of(value: object) -> CacheControl | None:
    """The cache breakpoint a block, message or tool carries, or None."""
    if isinstance(
        value,
        CacheableContentText | CacheableContentToolUse | CacheableToolMessage | CacheableToolParam,
    ):
        return value.cache_control
    return None


def without_breakpoint[T](value: T) -> T:
    """`value` with its cache breakpoint removed (the same value when it has none)."""
    if isinstance(value, CacheableContentText):
        return _plain(value, ContentText)  # type: ignore[return-value]
    if isinstance(value, CacheableContentToolUse):
        return _plain(value, ContentToolUse)  # type: ignore[return-value]
    if isinstance(value, CacheableToolMessage):
        return _plain(value, ToolMessage)  # type: ignore[return-value]
    if isinstance(value, CacheableToolParam):
        return _plain(value, ToolParam)  # type: ignore[return-value]
    return value


def edit_breakpoints(
    adaptation: AdaptedRequestBuilder,
    edit: Callable[[object, ChatMessage | None, CacheControl], CacheControl | None],
    noun: str,
) -> None:
    """Change or remove cache breakpoints, visiting them in the order a prompt renders them in
    (tools, then the context; a tool result's content before its own breakpoint, which ends
    after it). `edit` gets
    the marked tool, message or block, the message it is in (None for a tool, or for a message
    itself) and its breakpoint, and returns the breakpoint to keep, a changed one, or None to
    remove it. A breakpoint is a caching hint, so each change is a delivery loss."""
    changed = 0

    def visit[T](value: T, container: ChatMessage | None) -> T:
        nonlocal changed
        cache_control = breakpoint_of(value)
        if cache_control is None:
            return value
        kept = edit(value, container, cache_control)
        if kept == cache_control:
            return value
        changed += 1
        if kept is None:
            return without_breakpoint(value)
        return dataclasses.replace(value, cache_control=kept)  # type: ignore[call-arg]

    request = adaptation.request
    tools = tuple(visit(tool, None) for tool in request.tools)
    context: list[ChatMessage] = []
    for message in request.context:
        content: tuple[Any, ...] = tuple(visit(block, message) for block in message.content)
        if content != message.content:
            message = dataclasses.replace(message, content=content)
        if isinstance(message, ToolMessage):
            message = visit(message, None)
        context.append(message)
    if changed:
        adaptation.count("cache_breakpoint", noun, semantic=False, n=changed)
        adaptation.request = dataclasses.replace(request, tools=tools, context=tuple(context))


_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def cache_ttl_seconds(ttl: str) -> int | None:
    """A cache lifetime such as `5m` or `12h` in seconds, or None when `ttl` is not one."""
    match = re.fullmatch(r"(\d+)([smhd])", ttl)
    return int(match[1]) * _DURATION_UNITS[match[2]] if match else None


def fit_cache_ttls(adaptation: AdaptedRequestBuilder, supported: Sequence[str]) -> None:
    """Give each breakpoint a lifetime the destination offers (`supported`, shortest first).

    A TTL is a minimum: it becomes the shortest supported lifetime at least as long, else the
    longest supported. A TTL that is not a duration is cleared, so the breakpoint takes the
    destination's default, as an omitted TTL does.
    """

    def fit(_value: object, _container: object, cache_control: CacheControl) -> CacheControl:
        ttl = cache_control.ttl
        if ttl is None or ttl in supported:
            return cache_control
        seconds = cache_ttl_seconds(ttl)
        if seconds is None:
            return dataclasses.replace(cache_control, ttl=None)
        fitted = next(
            (s for s in supported if (cache_ttl_seconds(s) or 0) >= seconds), supported[-1]
        )
        return dataclasses.replace(cache_control, ttl=fitted)

    edit_breakpoints(
        adaptation, fit, "with a TTL the destination does not offer, fitted to one it does"
    )


def merge_tool_result_breakpoints(adaptation: AdaptedRequestBuilder, default_ttl: str) -> None:
    """Give a marked tool result whose final block is also marked the longer TTL of the two, on
    both: the two marks end at the same boundary. An omitted TTL counts as `default_ttl`. Runs
    before `fit_cache_ttls`, so the longer of the requested lifetimes is the one fitted."""

    def seconds(cache_control: CacheControl) -> int:
        return cache_ttl_seconds(cache_control.ttl or default_ttl) or 0

    def merge(value: object, container: object, cache_control: CacheControl) -> CacheControl:
        other = None
        if isinstance(value, CacheableToolMessage) and value.content:
            other = breakpoint_of(value.content[-1])
        elif (
            isinstance(container, CacheableToolMessage)
            and container.content
            and value is container.content[-1]
        ):
            other = container.cache_control
        return (
            other
            if other is not None and seconds(other) > seconds(cache_control)
            else cache_control
        )

    edit_breakpoints(
        adaptation, merge, "on a tool result and its final block, both given the longer TTL"
    )


def order_cache_ttls(adaptation: AdaptedRequestBuilder, default_ttl: str) -> None:
    """Shorten a TTL that is longer than one before it in render order to that one, for an API
    that rejects a longer TTL after a shorter one (Anthropic's). An omitted TTL counts as
    `default_ttl`."""
    shortest: list[tuple[int, str]] = []

    def in_order(_value: object, _container: object, cache_control: CacheControl) -> CacheControl:
        ttl = cache_control.ttl or default_ttl
        seconds = cache_ttl_seconds(ttl) or 0
        if shortest and seconds > shortest[0][0]:
            return dataclasses.replace(cache_control, ttl=shortest[0][1])
        if not shortest or seconds < shortest[0][0]:
            shortest[:] = [(seconds, ttl)]
        return cache_control

    edit_breakpoints(adaptation, in_order, "with a longer TTL after a shorter one, shortened")


def drop_breakpoints(
    adaptation: AdaptedRequestBuilder,
    which: Callable[[object, ChatMessage | None], bool],
    noun: str,
) -> None:
    """Remove the cache breakpoints `which` selects, given the marked tool, message or block and
    the message it is in (see `edit_breakpoints`)."""
    edit_breakpoints(
        adaptation,
        lambda value, container, cache_control: None if which(value, container) else cache_control,
        noun,
    )
