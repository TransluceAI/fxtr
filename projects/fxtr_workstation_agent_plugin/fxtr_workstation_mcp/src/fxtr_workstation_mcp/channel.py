"""Send ``notifications/claude/channel`` frames to the agent.

The contract (Claude Code channels reference): the MCP server
declares the ``experimental['claude/channel']`` capability at
init time, then emits ``notifications/claude/channel`` events
with ``params = {"content": str, "meta": {str: str}}``. Claude
Code renders each event into the agent's context as
``<channel source="<server-name>" <meta-attrs>>content</channel>``.
The ``source`` attribute is auto-set from the MCP server's
``name``; ``meta`` keys become tag attributes (identifiers
only — ``[A-Za-z0-9_]``).

Channel notifications are server-initiated: they fire when an
event happens (e.g. an ``InvokeResultEvent`` lands), which is
typically *outside* any active tool-call request context. The
high-level :meth:`mcp.server.session.ServerSession.send_notification`
API only accepts the typed ``ServerNotification`` union, which
doesn't include ``notifications/claude/channel``. So we write
the raw frame through the session's transport directly via
:attr:`ServerSession._write_stream` — private API, but the
official channels documentation explicitly endorses bypassing
the typed union for this case, and Anthropic's own channel
implementations do the same.

The session reference itself isn't request-scoped; it's tied to
the MCP client connection's lifetime. The lifespan captures
the current session on every tool call into ``AppContext``,
and this module reads it from there at notification time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification


class ChannelNotifier(Protocol):
    """Callable that pushes a ``notifications/claude/channel`` frame
    to the agent.

    The contract Claude Code renders is:
    ``<channel source="<server-name>" <attrs>>content</channel>``.
    The notifier takes the two pieces the caller controls:

    - ``content``: the text body that appears inside the
      ``<channel>…</channel>`` element. Free-form.
    - ``attrs``: a mapping whose entries become attributes on the
      rendered ``<channel …>`` element. Keys and values must be
      identifier-shaped (``[A-Za-z0-9_]``) per the channel
      contract. The notifier treats ``attrs`` as opaque — the
      agent-side rendering distinguishes shapes by inspecting
      attributes.

    Conventions for the two call sites today:

    - Slot-promotion (an invoke result / error landed): pass
      ``attrs={"slot_key": "...", "kind":
      "invoke_result" | "invoke_error"}``.
    - System events (e.g. supervisor crash + recovery): pass a
      different ``kind`` and omit ``slot_key``.
    """

    async def __call__(self, content: str, attrs: Mapping[str, str]) -> None: ...


#: Channel capability declaration to pass into
#: ``mcp.server.FastMCP`` (via the ``experimental_capabilities``
#: kwarg of ``create_initialization_options``). Required for
#: Claude Code to install the notification listener; without
#: it, channel frames are dropped silently.
CHANNEL_CAPABILITY: dict[str, dict[str, Any]] = {"claude/channel": {}}


def make_channel_notifier(
    get_session: Callable[[], Any | None],
) -> ChannelNotifier:
    """Build a :data:`ChannelNotifier` that emits frames through
    whatever session ``get_session()`` returns.

    The getter returns the most-recent ``ServerSession`` captured
    during a tool call (see ``AppContext.latest_session``). By
    construction, the workstation supervisor cannot fire a channel
    notification before that's been captured — it's started from
    inside a tool callable, after the session is captured — so the
    notifier asserts the getter returns a session rather than
    guarding against ``None``. A ``None`` here would indicate the
    invariant has been broken (a notification firing without any
    tool call having run), which warrants surfacing rather than
    dropping silently.
    """

    async def _notify(content: str, attrs: Mapping[str, str]) -> None:
        session = get_session()
        assert session is not None, (
            "channel notifier invoked with no captured ServerSession; "
            "see AppContext.latest_session — this should be impossible "
            "since the supervisor only starts from inside a tool call"
        )
        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={
                "content": content,
                "meta": dict(attrs),
            },
        )
        # The typed ServerNotification union doesn't include
        # notifications/claude/channel, so we can't go through the
        # high-level send_notification(). Push the raw frame
        # through the session's write stream directly — same
        # pattern Anthropic's own channel implementations use.
        await session._write_stream.send(SessionMessage(message=JSONRPCMessage(notification)))

    return _notify


def summarize_invoke_payload(payload: Any, *, max_len: int = 200) -> str:
    """Short structural digest of an invoke-result payload for the
    channel frame's ``content``. The full payload is in
    ``data_by_key[slot_key]`` if the agent wants details — this
    is just enough to nudge them to look."""
    rendered = repr(payload)
    if len(rendered) > max_len:
        return rendered[: max_len - 1] + "…"
    return rendered
