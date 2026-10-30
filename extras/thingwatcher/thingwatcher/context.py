"""Global context management for thingwatcher.

Provides a context variable to track the currently active Watcher instance.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .watcher import Watcher

_current_watcher: ContextVar[Watcher | None] = ContextVar(
    "thingwatcher_current_watcher", default=None
)


def get_current_watcher() -> Watcher | None:
    """Get the currently active watcher, or None if no watcher is active."""
    return _current_watcher.get()


def set_current_watcher(watcher: Watcher | None) -> Token[Watcher | None]:
    """Set the current watcher and return a token to restore the previous state."""
    return _current_watcher.set(watcher)


def reset_current_watcher(token: Token[Watcher | None]) -> None:
    """Reset the current watcher to its previous state using the token."""
    _current_watcher.reset(token)
