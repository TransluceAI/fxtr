"""Bounded LRU for full script-exception tracebacks.

When a ``run_script`` raises, the host-side formatter compresses
the traceback to a two-frame summary (see
:func:`ws_connection._compress_script_traceback`) and stores the
full :func:`traceback.format_exception` string here keyed by a
fresh ``exc_id``. The tool result tells the agent the ``exc_id``
so they can fetch the full form from a follow-up ``run_script``
via the sandbox builtin ``get_full_exception(exc_id)``.

Lifespan-scoped on :class:`AppContext` — one log per MCP process,
shared across all sessions / supervisors / scripts. IDs never
recycle (the counter only increments); on overflow the oldest
entry falls off the LRU and its ID becomes a 404 to
``get_full_exception``.

Single-threaded API (anyio): no locking needed; every caller
runs on the same event loop.
"""

from __future__ import annotations

from collections import OrderedDict


#: Max entries to retain. Hitting the cap drops the oldest
#: traceback (its ``exc_id`` becomes invalid). 20 covers the
#: typical "a few errors in a row while debugging" pattern
#: without letting the log grow unbounded across long sessions.
DEFAULT_MAX_ENTRIES = 20


class ExceptionLog:
    """In-memory LRU of full traceback strings, keyed by
    monotonic ``exc_id`` (``"exc_1"``, ``"exc_2"``, …).

    Operations:

    - :meth:`store` — append a new entry, return its ``exc_id``.
      Evicts the oldest if the log is at capacity.
    - :meth:`get` — return the stored traceback or ``None`` if
      the id was never stored or has been evicted.

    Ordering: insertion order. ``OrderedDict`` lets eviction be
    ``popitem(last=False)`` regardless of access patterns — we
    deliberately do **not** promote on read, so a long-lived
    traceback the agent keeps re-fetching can still be evicted
    by 20 newer failures. (The agent should copy the string out
    of ``get_full_exception`` if they want to keep it longer.)
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive; got {max_entries}")
        self._max_entries = max_entries
        self._counter = 0
        self._entries: OrderedDict[str, str] = OrderedDict()

    def store(self, full_traceback: str) -> str:
        """Append ``full_traceback`` to the log, return the new
        ``exc_id`` (e.g. ``"exc_1"``). On overflow, evict the
        oldest entry first."""
        self._counter += 1
        exc_id = f"exc_{self._counter}"
        self._entries[exc_id] = full_traceback
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return exc_id

    def get(self, exc_id: str) -> str | None:
        """Return the stored traceback for ``exc_id``, or ``None``
        if absent (never stored, or evicted)."""
        return self._entries.get(exc_id)

    def __len__(self) -> int:
        return len(self._entries)
