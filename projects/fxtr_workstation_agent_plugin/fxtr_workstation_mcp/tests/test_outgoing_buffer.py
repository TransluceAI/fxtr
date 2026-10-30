"""Unit tests for :mod:`fxtr_workstation_mcp.outgoing_buffer`.

Exercises the buffer in isolation (no workstation, no FastMCP):

- Render format (attribute alphabetization, no escaping).
- ``parse_events`` round-trip — parses what the renderer emits.
- ``add`` + ``tool_window`` interaction: notifications buffered
  during a tool window, drained on exit, depth bookkeeping.
- Debounced flush sends one channel frame for all pending
  messages and resets the scheduled flag.
- A tool starting during the debounce window steals the drain
  and the flush returns without sending.
- ``watch_slot`` fires the registered :class:`anyio.Event` on
  the next matching ``add``; ``unwatch_slot`` releases unfired
  watchers.

Integration coverage (events landing in real tool drains /
channel frames) lives in the e2e tests; this file keeps the
mechanics honest at the unit level."""

from __future__ import annotations

from collections.abc import Mapping

import anyio
import pytest

from fxtr_workstation_mcp.outgoing_buffer import (
    Message,
    OutgoingBuffer,
    ScriptMessage,
    _render_message,
    _render_script_message,
)

from ._events import parse_events

pytestmark = pytest.mark.anyio


# --- Render format --------------------------------------------------


async def test_render_message_attrs_alphabetized() -> None:
    m = Message(content="hello", attrs={"slot_key": "r", "kind": "invoke_result"})
    rendered = _render_message(m)
    assert rendered == '<event kind="invoke_result" slot_key="r">hello</event>'


async def test_render_message_empty_attrs() -> None:
    m = Message(content="bare body", attrs={})
    assert _render_message(m) == "<event>bare body</event>"


async def test_render_message_no_escaping() -> None:
    """Content is passed through as-is — no HTML-escaping. Matches
    today's channel-frame contract; revisit only if the agent's
    renderer chokes on something."""
    m = Message(content='has > and " in it', attrs={"kind": "x"})
    assert _render_message(m) == '<event kind="x">has > and " in it</event>'


async def test_render_script_message() -> None:
    assert (
        _render_script_message(ScriptMessage(content="hello"))
        == "<script_result>hello</script_result>"
    )
    multiline = ScriptMessage(content="a\n--- stdout ---\n")
    assert _render_script_message(multiline) == "<script_result>a\n--- stdout ---\n</script_result>"


async def test_render_parse_round_trip() -> None:
    m = Message(content="payload", attrs={"slot_key": "s1", "kind": "callback"})
    parsed = parse_events(_render_message(m))
    assert len(parsed) == 1
    assert parsed[0].attrs == {"slot_key": "s1", "kind": "callback"}
    assert parsed[0].content == "payload"


# --- Buffer mechanics -----------------------------------------------


class _FakeNotifier:
    """Records every channel frame sent by the buffer's debounced
    flush. Drop-in for :data:`ChannelNotifier`."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, content: str, attrs: Mapping[str, str]) -> None:
        self.sends.append((content, dict(attrs)))


async def test_tool_window_drains_inline_no_flush() -> None:
    """Inside a tool window, ``add`` buffers and ``drain`` returns
    the rendered events. The debounced flush task is never
    scheduled."""
    async with anyio.create_task_group() as tg:
        notif = _FakeNotifier()
        buf = OutgoingBuffer(notify_channel=notif, task_group=tg)
        with buf.tool_window() as drain:
            buf.add("first", {"slot_key": "a", "kind": "invoke_result"})
            buf.add("second", {"slot_key": "b", "kind": "invoke_result"})
            assert buf._flush_scheduled is False  # type: ignore[attr-defined]
            drained = drain()
        # Sleep past the would-be debounce window to confirm no
        # background flush fires.
        await anyio.sleep(0.3)
        assert notif.sends == []
    parsed = parse_events(drained)
    assert [(e.attrs, e.content) for e in parsed] == [
        ({"kind": "invoke_result", "slot_key": "a"}, "first"),
        ({"kind": "invoke_result", "slot_key": "b"}, "second"),
    ]


async def test_tool_window_drains_script_result_before_events() -> None:
    """``add_script_result`` then ``add`` preserves insertion order."""
    async with anyio.create_task_group() as tg:
        notif = _FakeNotifier()
        buf = OutgoingBuffer(notify_channel=notif, task_group=tg)
        with buf.tool_window() as drain:
            buf.add_script_result("Script ran. Committed: 1 noop.")
            buf.add("evt", {"slot_key": "a", "kind": "invoke_result"})
            drained = drain()
        await anyio.sleep(0.2)
        assert notif.sends == []
    lines = drained.split("\n")
    assert lines[0] == "<script_result>Script ran. Committed: 1 noop.</script_result>"
    parsed = parse_events(drained)
    assert len(parsed) == 1
    assert parsed[0].attrs == {"kind": "invoke_result", "slot_key": "a"}


async def test_idle_add_triggers_debounced_flush() -> None:
    """Outside a tool window, ``add`` schedules a debounced flush
    that delivers all pending events in one channel frame."""
    async with anyio.create_task_group() as tg:
        notif = _FakeNotifier()
        buf = OutgoingBuffer(notify_channel=notif, task_group=tg, debounce_seconds=0.05)
        buf.add("first", {"slot_key": "a", "kind": "invoke_result"})
        buf.add("second", {"slot_key": "b", "kind": "callback"})
        # Both adds happened while idle; only one flush should
        # have been scheduled.
        assert buf._flush_scheduled is True  # type: ignore[attr-defined]
        await anyio.sleep(0.15)
        assert len(notif.sends) == 1
        content, attrs = notif.sends[0]
        assert attrs == {"kind": "events"}
        parsed = parse_events(content)
        assert len(parsed) == 2
        assert parsed[0].attrs["slot_key"] == "a"
        assert parsed[1].attrs["slot_key"] == "b"
        # After flush completes, the flag is cleared so future
        # adds can schedule again.
        assert buf._flush_scheduled is False  # type: ignore[attr-defined]


async def test_tool_starts_during_debounce_window_steals_drain() -> None:
    """If a tool's ``tool_window`` opens during the debounce
    sleep, the flush task should bail (the tool will drain on
    exit). No channel frame goes out."""
    async with anyio.create_task_group() as tg:
        notif = _FakeNotifier()
        buf = OutgoingBuffer(notify_channel=notif, task_group=tg, debounce_seconds=0.2)
        buf.add("pre-tool", {"slot_key": "a", "kind": "invoke_result"})
        # Open a tool window before the flush wakes up.
        await anyio.sleep(0.05)
        with buf.tool_window() as drain:
            # Wait long enough for the flush task to see depth>0
            # and bail.
            await anyio.sleep(0.25)
            drained = drain()
        await anyio.sleep(0.1)
        assert notif.sends == []
    assert parse_events(drained)[0].attrs == {
        "kind": "invoke_result",
        "slot_key": "a",
    }


async def test_watch_slot_fires_on_matching_add() -> None:
    """``watch_slot`` returns an :class:`anyio.Event` that
    fires the first time an ``add`` arrives with a matching
    ``slot_key``. Subsequent adds for the same slot do *not*
    re-fire (one-shot)."""
    async with anyio.create_task_group() as tg:
        notif = _FakeNotifier()
        buf = OutgoingBuffer(notify_channel=notif, task_group=tg)
        ev = buf.watch_slot("target")
        assert not ev.is_set()
        buf.add("nope", {"slot_key": "other", "kind": "invoke_result"})
        assert not ev.is_set()
        buf.add("hit", {"slot_key": "target", "kind": "invoke_result"})
        assert ev.is_set()
        # Watchers are one-shot: registry cleared on fire.
        # A new watcher gets a fresh event.
        ev2 = buf.watch_slot("target")
        assert not ev2.is_set()
        buf.add("hit-again", {"slot_key": "target", "kind": "callback"})
        assert ev2.is_set()


async def test_unwatch_slot_releases_unfired_watcher() -> None:
    async with anyio.create_task_group() as tg:
        notif = _FakeNotifier()
        buf = OutgoingBuffer(notify_channel=notif, task_group=tg)
        ev = buf.watch_slot("target")
        buf.unwatch_slot("target", ev)
        # Adding for that slot now must not set ``ev``.
        buf.add("post-unwatch", {"slot_key": "target", "kind": "invoke_result"})
        assert not ev.is_set()


async def test_multiple_watchers_same_slot_all_fire() -> None:
    async with anyio.create_task_group() as tg:
        notif = _FakeNotifier()
        buf = OutgoingBuffer(notify_channel=notif, task_group=tg)
        ev1 = buf.watch_slot("target")
        ev2 = buf.watch_slot("target")
        buf.add("hello", {"slot_key": "target", "kind": "invoke_result"})
        assert ev1.is_set()
        assert ev2.is_set()


async def test_tool_window_nested_depth() -> None:
    """Nested tool windows should suppress flushes until the
    outermost exits — but in practice, FastMCP's tool dispatch
    serializes calls (one tool at a time). The depth-counter
    model handles concurrent calls if they ever happen."""
    async with anyio.create_task_group() as tg:
        notif = _FakeNotifier()
        buf = OutgoingBuffer(notify_channel=notif, task_group=tg, debounce_seconds=0.05)
        with buf.tool_window() as drain_outer:
            with buf.tool_window() as drain_inner:
                buf.add("inside", {"slot_key": "a", "kind": "invoke_result"})
                # Inner drain takes it; outer drain is empty.
                got_inner = drain_inner()
                assert "inside" in got_inner
            # Inner exited; depth = 1. Still no flush scheduled.
            assert buf._flush_scheduled is False  # type: ignore[attr-defined]
            buf.add("outer", {"slot_key": "b", "kind": "invoke_result"})
            assert buf._flush_scheduled is False  # type: ignore[attr-defined]
            got_outer = drain_outer()
            assert "outer" in got_outer
        # Both exits done. No add after — nothing to flush.
        await anyio.sleep(0.1)
        assert notif.sends == []


async def test_idle_flush_after_failed_send_logs_and_drops() -> None:
    """If the underlying ``notify_channel`` raises during the
    debounced flush, the buffer logs (we can't easily intercept
    that) but continues operating — the next add schedules
    another flush."""

    class _BadNotifier:
        call_count = 0

        async def __call__(self, content: str, attrs: Mapping[str, str]) -> None:
            type(self).call_count += 1
            if type(self).call_count == 1:
                raise RuntimeError("simulated transport failure")

    async with anyio.create_task_group() as tg:
        bad = _BadNotifier()
        buf = OutgoingBuffer(notify_channel=bad, task_group=tg, debounce_seconds=0.05)  # type: ignore[arg-type]
        buf.add("first", {"slot_key": "a", "kind": "invoke_result"})
        await anyio.sleep(0.15)
        # First flush raised; flag should be cleared so the next add
        # can schedule again.
        assert buf._flush_scheduled is False  # type: ignore[attr-defined]
        buf.add("second", {"slot_key": "b", "kind": "invoke_result"})
        await anyio.sleep(0.15)
        # Second send should have happened.
        assert _BadNotifier.call_count == 2
