"""ClydraManager - The main tracked thing representing a Claude Code session."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeSDKClient


@dataclass
class ClydraManager:
    """Manager for a Claude Code session.

    This is the single tracked thing with key ["clydra_root"].

    Attributes:
        session_id: The current SDK session ID (set after first response).
        transcript_path: Path to the session transcript file (set from hook).
        state: Current state - "setup", "streaming", or "waiting_input".
        cwd: Working directory for Claude Code.
    """

    session_id: str | None = None
    transcript_path: str | None = None
    state: Literal["setup", "streaming", "waiting_input"] = "setup"
    cwd: Path = field(default_factory=Path.cwd)

    # Private events list - use methods to modify
    _events: list[dict[str, Any]] = field(default_factory=list, repr=False)

    # Pending ops for snapshotter - tracks push/pop/clear operations
    _pending_ops: list[dict[str, Any]] = field(default_factory=list, repr=False)

    # Runtime only (not serialized) - prefixed with underscore
    _client: ClaudeSDKClient | None = field(default=None, repr=False)
    _transcript_loaded: bool = field(default=False, repr=False)

    def append_event(self, event: dict[str, Any]) -> None:
        """Append a single event to the events list."""
        self._events.append(event)
        self._pending_ops.append({"op": "push", "count": 1})

    def append_events(self, events: list[dict[str, Any]]) -> None:
        """Append multiple events to the events list."""
        if events:
            self._events.extend(events)
            self._pending_ops.append({"op": "push", "count": len(events)})

    def clear_events(self) -> None:
        """Clear all events from the events list."""
        self._events = []
        self._pending_ops.append({"op": "clear"})

    def pop_events(self, n: int) -> None:
        """Pop n events from the end of the events list."""
        if n > 0 and n <= len(self._events):
            self._events = self._events[:-n]
            self._pending_ops.append({"op": "pop", "count": n})
        else:
            raise ValueError(
                f"Cannot pop {n} events from events list of length {len(self._events)}"
            )

    def get_events(self) -> list[dict[str, Any]]:
        """Get read-only access to the events list."""
        return self._events

    def _consume_pending_ops(self) -> list[dict[str, Any]]:
        """Consume and return pending ops (for snapshotter use only)."""
        ops = self._pending_ops
        self._pending_ops = []
        return ops
