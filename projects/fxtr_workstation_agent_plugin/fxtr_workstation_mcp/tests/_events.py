"""Shared test helpers for inspecting the agent-visible event stream.

Every notification the MCP surfaces lands as a
``<event {attrs}>{content}</event>`` block in one of two places:

- **Inside a tool's response string** when the event arrived while
  that tool's :meth:`OutgoingBuffer.tool_window` was open.
- **Inside a ``notifications/claude/channel`` frame** when no tool
  was active at event time. The frame's outer attrs are
  ``{"kind": "events"}`` and the body holds one or more
  ``<event …>`` blocks (newline-joined).

These helpers parse and aggregate events from both sources so a
test can simply ask "did the agent see a ``kind=callback`` event
for slot ``cb``?" without caring which path delivered it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedEvent:
    """One parsed ``<event …>…</event>`` block.

    ``attrs`` is the parsed attribute dict; ``content`` is the body
    between the open and close tags. Both are raw — no unescaping
    (consistent with the renderer's no-escape contract).
    """

    content: str
    attrs: dict[str, str]


_EVENT_RE = re.compile(r"<event([^>]*)>(.*?)</event>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def parse_events(text: str) -> list[ParsedEvent]:
    """Pull every ``<event …>…</event>`` block out of ``text``.

    Order in the returned list matches order in the text. Empty
    or ``<event>``-free text → empty list. Multiple events in one
    line / on consecutive lines are all caught (regex is
    non-greedy via ``re.DOTALL``).
    """
    out: list[ParsedEvent] = []
    for m in _EVENT_RE.finditer(text):
        attr_str = m.group(1)
        content = m.group(2)
        attrs = dict(_ATTR_RE.findall(attr_str))
        out.append(ParsedEvent(content=content, attrs=attrs))
    return out


def parse_events_from_sources(
    *,
    channel_frame_contents: Iterable[str],
    tool_outputs: Iterable[str],
) -> list[ParsedEvent]:
    """Aggregate parsed events from both delivery paths.

    Order: every channel frame's events first (in capture order),
    then every tool output's events (in capture order). Tests
    typically don't care about cross-source order; if they do,
    inspect each source directly via :func:`parse_events`.
    """
    out: list[ParsedEvent] = []
    for c in channel_frame_contents:
        out.extend(parse_events(c))
    for s in tool_outputs:
        out.extend(parse_events(s))
    return out
