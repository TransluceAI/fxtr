"""Buffered delivery of agent-facing messages.

Single transport-layer abstraction that serializes notifications
and tool-result drains through one stream. Owned by
``AppContext`` (lifespan-scoped). The :class:`WorkstationConnection`
supervisor's ``_notify_channel`` callable is wired to push into
:meth:`OutgoingBuffer.add` instead of writing raw
``notifications/claude/channel`` frames directly.

Three delivery shapes:

- **In-tool drain.** While a tool's body is wrapped in
  :meth:`tool_window`, notifications buffer locally and the tool
  appends ``drain()`` to its result string. The agent sees the
  events inline next to the tool that triggered them.
- **Debounced channel flush.** When no tool is active, the first
  ``add()`` or :meth:`~OutgoingBuffer.add_script_result` schedules a
  flush task; it sleeps for ``debounce_seconds`` and then sends a
  single ``notifications/claude/channel`` frame containing every
  buffered row (``<event …>…</event>`` and/or
  ``<script_result>…</script_result>``). If a tool starts during the
  debounce window, the flush bails and lets the tool drain.
- **Script tool primary.** MCP :func:`~fxtr_workstation_mcp.server.run_script`
  pushes its primary text via :meth:`OutgoingBuffer.add_script_result`
  alongside ``<event>`` notifications inside the tool drain ordering.
- **Slot watchers.** The ``wait_for_slot_notification`` MCP tool
  registers an :class:`anyio.Event` keyed by ``slot_key``; the
  first notification with a matching ``slot_key`` attribute sets
  the event so the tool wakes and drains.

Rendering: normal :class:`Message` rows become
``<event {sorted_attrs}>{content}</event>`` at drain/flush time.
:class:`ScriptMessage` rows become ``<script_result>{content}</script_result>``.
No HTML-escaping (consistent with the existing channel-frame
contract); attribute keys/values are identifier-shaped by
contract.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
import anyio
import anyio.abc

from .channel import ChannelNotifier

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Message:
    """One buffered (content, attrs) pair, rendered as
    ``<event …>…</event>`` at output time."""

    content: str
    attrs: dict[str, str]


def _render_message(m: Message) -> str:
    """Render one :class:`Message` as a single ``<event …>…</event>``
    line. Attribute keys are alphabetized for determinism;
    attribute values and content are passed through as-is (no
    escaping)."""
    attr_str = "".join(f' {k}="{m.attrs[k]}"' for k in sorted(m.attrs))
    return f"<event{attr_str}>{m.content}</event>"


@dataclass(frozen=True)
class ScriptMessage:
    """Tool-primary script output buffered for :meth:`drain`; renders as
    ``<script_result>…</script_result>`` (no attributes)."""

    content: str


def _render_script_message(m: ScriptMessage) -> str:
    return f"<script_result>{m.content}</script_result>"


def _render_pending_item(item: Message | ScriptMessage) -> str:
    if isinstance(item, ScriptMessage):
        return _render_script_message(item)
    return _render_message(item)


@dataclass
class OutgoingBuffer:
    """Lifespan-scoped buffer for outgoing agent messages.

    Constructed once in the FastMCP lifespan body; lives until
    lifespan exit. Survives :class:`WorkstationConnection`
    supervisor reconnects (so buffered messages aren't lost when
    the workstation /client connection blips).

    Construction:

    - ``notify_channel`` — underlying transport sender used by the
      debounced-flush path. The lifespan wires this to
      ``make_channel_notifier(...)`` so a successful flush actually
      hits the agent's stdio.
    - ``task_group`` — anyio TG used to schedule the debounced
      flush task. Lifespan's TG works; the flush task is short-
      lived (sleep + one send).
    - ``debounce_seconds`` — flush delay when adding a notification
      while no tool is active. Default 0.1.
    """

    notify_channel: ChannelNotifier
    task_group: anyio.abc.TaskGroup
    debounce_seconds: float = 0.1

    _pending: list[Message | ScriptMessage] = field(default_factory=list)
    _tool_active_depth: int = 0
    _flush_scheduled: bool = False
    _slot_watchers: dict[str, list[anyio.Event]] = field(default_factory=dict)

    # --- Public API -------------------------------------------------

    def add(self, content: str, attrs: Mapping[str, str]) -> None:
        """Buffer one (content, attrs) message.

        Synchronous and side-effect-free w.r.t. async waits — safe
        to call from any task without ``await``. Fires any
        registered slot watchers whose ``slot_key`` matches
        ``attrs["slot_key"]``. If no tool is active and no flush
        is already scheduled, kicks off a debounced flush task on
        the buffer's task group.

        The supervisor's ``_notify_channel`` callable is bridged
        to this method via a thin async adapter in
        ``server.lifespan`` so the supervisor's existing
        ``await self._notify_channel(...)`` call sites don't need
        to change.
        """
        msg = Message(content=content, attrs=dict(attrs))
        self._pending.append(msg)
        slot_key = attrs.get("slot_key")
        if slot_key is not None:
            events = self._slot_watchers.pop(slot_key, [])
            for ev in events:
                ev.set()
        self._schedule_debounced_flush_if_idle()

    def add_script_result(self, content: str) -> None:
        """Buffer ``run_script`` (and similar) primary output as a
        :class:`ScriptMessage`. Rendering is
        ``<script_result>{content}</script_result>`` at drain time.

        Empty or whitespace-only ``content`` is ignored. Does not fire
        slot watchers. Uses the same depth / debounced-flush scheduling
        rules as :meth:`add`."""
        if not content.strip():
            return
        self._pending.append(ScriptMessage(content=content))
        self._schedule_debounced_flush_if_idle()

    @contextlib.contextmanager
    def tool_window(self) -> Iterator[Callable[[], str]]:
        """Use around a tool body. While open, ``add()`` buffers
        without scheduling a flush, so the tool can drain at exit.

        Yields a ``drain()`` callable that returns the current
        buffer's contents rendered as a single string (newline-
        joined ``<event …>`` rows and ``<script_result>…``
        wrappers) and clears the buffer.

        Sync ctx-mgr — depth bookkeeping has no awaits and using a
        sync cm keeps exception-handling semantics clean (the
        ``finally`` runs regardless of how the body exits).
        """
        self._tool_active_depth += 1
        try:
            yield self._drain_to_text
        finally:
            self._tool_active_depth -= 1

    def watch_slot(self, slot_key: str) -> anyio.Event:
        """Register an :class:`anyio.Event` that fires the next
        time a message with ``attrs["slot_key"] == slot_key`` is
        added. Multiple concurrent watchers for the same key are
        fine — all fire on the next matching add.

        Watchers are one-shot: after firing they're removed from
        the watcher list. Callers that didn't fire (timeout, etc.)
        should call :meth:`unwatch_slot` to release the entry.
        """
        ev = anyio.Event()
        self._slot_watchers.setdefault(slot_key, []).append(ev)
        return ev

    def unwatch_slot(self, slot_key: str, ev: anyio.Event) -> None:
        """Drop a watcher registered by :meth:`watch_slot`. Safe to
        call whether or not the event has already fired (firing
        removes the entry; this call is a no-op then)."""
        watchers = self._slot_watchers.get(slot_key)
        if watchers is None:
            return
        try:
            watchers.remove(ev)
        except ValueError:
            pass
        if not watchers:
            self._slot_watchers.pop(slot_key, None)

    # --- Internals --------------------------------------------------

    def _schedule_debounced_flush_if_idle(self) -> None:
        """When no tool window is active, schedule exactly one debounced
        flush unless one is already pending.

        Matches :meth:`add` / :meth:`add_script_result` shared behavior."""
        if self._tool_active_depth > 0:
            return
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        try:
            self.task_group.start_soon(self._debounced_flush)
        except RuntimeError:
            # Task group is closing or cancelled (lifespan teardown).
            # Leave ``_flush_scheduled`` set so we don't keep
            # retrying; the messages are stranded but the MCP is
            # going away anyway.
            _log.warning(
                "outgoing_buffer: could not schedule debounced flush "
                "(task group not accepting new tasks)"
            )

    def _drain_to_text(self) -> str:
        """Drain ``_pending`` to a single string. Empty buffer →
        empty string. Multi-message drain → newline-joined
        rendered rows (``<event …>`` or ``<script_result>…``)."""
        if not self._pending:
            return ""
        msgs = self._pending
        self._pending = []
        return "\n".join(_render_pending_item(m) for m in msgs)

    async def _debounced_flush(self) -> None:
        """Background task spawned by :meth:`add` when the buffer
        transitions from empty to non-empty with no tool active.

        Sleeps ``debounce_seconds``, then either drains + sends a
        single channel frame (if no tool started during the sleep)
        or yields the drain to the tool that did start.

        On send failure: log + drop the batch. Rationale in the
        plan doc; basically, if stdio is broken we can't recover.
        """
        try:
            await anyio.sleep(self.debounce_seconds)
        except BaseException:
            self._flush_scheduled = False
            raise
        if self._tool_active_depth > 0:
            # A tool started during the sleep. It'll drain on exit;
            # we just clear the scheduled flag.
            self._flush_scheduled = False
            return
        text = self._drain_to_text()
        self._flush_scheduled = False
        if not text:
            return
        try:
            await self.notify_channel(text, {"kind": "events"})
        except Exception:
            _log.exception(
                "outgoing_buffer: failed to deliver debounced channel frame; "
                "dropping %d buffered message(s)",
                text.count("\n") + 1,
            )
