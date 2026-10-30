"""The change-hint accumulator: ambient collection of invalidation hints.

The emission half of the change-event stream (``notes/monitoring_ui.md``):
mutating code — session bodies and the marking walks alike, however deep —
reports what it touched with a local :func:`note_change` call, and the
session flushes the deduped set to its listener after the transaction
commits. The accumulator is a :class:`~contextvars.ContextVar` holding a
plain mutable set, installed by :func:`collect_hints` around each session
op; anyio propagates the context into ``to_thread`` workers, so bodies
running in the session's worker thread write into the same set the async
side flushes. No function signatures change to participate: any call site
that manipulates workflow state just notes what it did.

Hints are **coarse, at-least-once invalidation facts** — kind + grid path,
no payloads. They are internal vocabulary (never on the wire): the watch
engine (`watch_engine.py`) consumes them to decide which subscriptions
recompute — see ``notes/event_system.md``. The kinds:

- ``("grids", "")`` — the set of grids changed (create / delete /
  wholesale import): refetch the namespace summary.
- ``("cells", <grid path>)`` — displayed state for that grid may have
  changed: contents, generations/statuses, or **derived verdicts** — the
  stamp-clearing walks note every grid whose stamps they clear, so a
  mutation's transitive invalidation fan arrives as per-grid hints.
- ``("intent", "")`` — sourcing records or targets changed.
- ``("bindings", "")`` — a handle binding changed. Ephemeral
  binds/releases and sandbox launch-state transitions (launching /
  running / faulted / shutdown) note this host-side
  (`bindings.BindingHost`'s change listener — runtime state, not
  session state); durable *sandboxed* binding mutations note it from
  their session bodies through this collector, like any other session
  change. Same vocabulary either way.

Noting with no collector installed is a silent no-op (library code touched
outside a session op — e.g. the import loader's internals under its own
composite — still works; the enclosing op owns the flush).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

Hint = tuple[str, str]
"""One invalidation hint: ``(kind, grid path)`` — path ``""`` for the
path-less kinds (see the module docstring's vocabulary)."""

_PENDING: ContextVar[set[Hint] | None] = ContextVar("wf3_pending_hints", default=None)


def note_change(kind: str, path: str = "") -> None:
    """Record one hint into the ambient accumulator (no-op when none is
    installed).

    Args:
        kind: The hint kind (``grids`` / ``cells`` / ``intent``).
        path: The grid's key-path text, for ``cells`` hints.
    """
    acc = _PENDING.get()
    if acc is not None:
        acc.add((kind, path))


@contextmanager
def collect_hints() -> Iterator[set[Hint]]:
    """Install a fresh accumulator for the duration of one session op.

    The caller flushes the yielded set to its listener *after* the block
    exits cleanly (post-commit); an exception propagating out of the
    block (a rolled-back transaction) simply skips the flush, so a hint
    for a change that never became visible cannot escape. Nesting
    installs a fresh inner set (the inner op owns its own flush).
    """
    acc: set[Hint] = set()
    token = _PENDING.set(acc)
    try:
        yield acc
    finally:
        _PENDING.reset(token)
