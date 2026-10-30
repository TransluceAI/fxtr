"""Always-on hooks the scope-drain report reads (leaf module).

This module holds the cheap, always-recorded state behind
``fxtr2.debug.scope_report``: the live-scope registry, the
"which task activated the current ``ACTIVE_SCOPE``" record, and the
capture helpers for scope creation sites/tracebacks. It is a **leaf**
— it imports nothing from ``fxtr2.data``/``fxtr2.services`` — so the
core modules (``entity_ref_scope``, ``service_cap_common``) can import
it at module level without cycles. Everything here must stay cheap:
these hooks run on scope construction/entry and on every
``ACTIVE_SCOPE`` activation (including each service dispatch).

The one env var: ``FXTR_SCOPE_REPORT_TB=1`` enables *both* traceback
captures for a debugging rerun — it sets the existing
``fxtr2.leases.local_lease.LEASE_LEAK_TRACK_ALLOCATIONS`` flag (who
took each lease) and this module's ``CAPTURE_CREATION_TB`` (who
created each scope). Both can also be flipped programmatically.
"""

from __future__ import annotations

import contextvars
import os
import sys
import time
import traceback
import weakref
from typing import TYPE_CHECKING, Any

from ..leases import local_lease

if TYPE_CHECKING:
    from ..data.entity_ref_scope import FxtrScope

CAPTURE_CREATION_TB: bool = False
"""If True, capture a traceback at every ``FxtrScope`` construction (reported by the
scope-drain report). Flipped by ``FXTR_SCOPE_REPORT_TB=1`` at import, or programmatically."""

if os.environ.get("FXTR_SCOPE_REPORT_TB") == "1":
    CAPTURE_CREATION_TB = True
    local_lease.LEASE_LEAK_TRACK_ALLOCATIONS = True


monotonic = time.monotonic
"""Re-exported so instrumented modules need no extra stdlib import."""


LIVE_SCOPES: "weakref.WeakSet[FxtrScope]" = weakref.WeakSet()
"""Every started ``FxtrScope`` (added in ``_do_open``). Weak: scopes leave on GC; the
report additionally filters on open/closed state, so a closed-but-referenced scope is
only listed as history in the inventory, never as wedged."""


#: The activation record: set alongside every ``ACTIVE_SCOPE.set`` with
#: (weakref-to-scope, weakref-to-activating-task-or-None, task label). Read at
#: non-isolated scope construction to classify the implicit parent capture as
#: same-task or fork-inherited (task context copies carry this record into
#: child tasks, exactly like ``ACTIVE_SCOPE`` itself — that is the point).
_ACTIVATION: contextvars.ContextVar[
    "tuple[weakref.ref[Any], weakref.ref[Any] | None, str] | None"
] = contextvars.ContextVar("fxtr_scope.debug.ACTIVATION", default=None)


def _current_task() -> Any | None:
    """The current asyncio task, or None outside a running asyncio loop.

    asyncio-only on purpose: this is debug metadata, ``asyncio.current_task``
    is the cheap call, and a non-asyncio backend degrades to "unknown task"
    (classified as not-cross-task) rather than an error.
    """
    import asyncio

    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def push_activation(scope: "FxtrScope") -> contextvars.Token[Any]:
    """Record that ``scope`` was just made active in the current task.

    Called (paired with :func:`pop_activation`) immediately after every
    ``ACTIVE_SCOPE.set`` in the core, so the record and the contextvar
    travel together — including into forked task contexts.

    Args:
        scope: The scope that was just made active.

    Returns:
        The contextvar token to hand back to :func:`pop_activation`
        when the activation ends.
    """
    task = _current_task()
    record = (
        weakref.ref(scope),
        weakref.ref(task) if task is not None else None,
        repr(getattr(task, "get_name", lambda: task)()) if task is not None else "<no task>",
    )
    return _ACTIVATION.set(record)


def pop_activation(token: contextvars.Token[Any]) -> None:
    """Undo the matching :func:`push_activation`.

    Args:
        token: The token that ``push_activation`` returned.
    """
    _ACTIVATION.reset(token)


def classify_implicit_capture(captured_scope: "FxtrScope | None") -> bool:
    """True iff the implicit active-scope capture happening *now* is fork-inherited.

    Called from ``FxtrScope._init_common`` for non-isolated construction,
    immediately after it captured ``captured_scope`` from ``ACTIVE_SCOPE``.
    Fork-inherited means: the recorded activation of that same scope happened
    in a *different* task than the one constructing this scope — i.e. the
    ambient scope arrived through task-context inheritance, the hazard
    ingredient the drain report's classification exists to name.

    Args:
        captured_scope: The scope just captured as the implicit parent
            (``None`` classifies False).

    Returns:
        True iff the capture is confidently fork-inherited. Unknown
        situations (no activation record, a record for a different
        scope — e.g. the ``GLOBAL_SCOPE`` default was never explicitly
        activated — or an unknown activating task) classify False.
    """
    record = _ACTIVATION.get()
    if record is None or captured_scope is None:
        return False
    scope_ref, task_ref, _label = record
    if scope_ref() is not captured_scope:
        return False
    if task_ref is None:
        return False
    return task_ref() is not _current_task()


_INSTRUMENTED_FILES: tuple[str, ...] = ()
"""Filenames whose frames ``creation_site`` skips (the scope module itself);
registered by the instrumented module at import via ``register_internal_file``."""


def register_internal_file(filename: str) -> None:
    """Mark a module's frames as internal for :func:`creation_site`.

    Args:
        filename: The module's ``__file__`` — its frames are skipped
            when walking to the user's construction site. Called at
            import by the instrumented core module(s).
    """
    global _INSTRUMENTED_FILES
    if filename not in _INSTRUMENTED_FILES:
        _INSTRUMENTED_FILES = (*_INSTRUMENTED_FILES, filename)


def creation_site() -> str | None:
    """The nearest caller frame outside the instrumented core modules.

    One short frame walk per scope construction (always-on; measured
    sub-microsecond — see the extra8 part-1 perf note).

    Returns:
        The site as ``"<filename>:<lineno>"``, or None if no frame
        outside the registered internal files exists within the walk
        limit.
    """
    try:
        frame = sys._getframe(2)
    except ValueError:
        return None
    for _ in range(12):
        if frame is None:
            return None
        filename = frame.f_code.co_filename
        if filename not in _INSTRUMENTED_FILES:
            return f"{filename}:{frame.f_lineno}"
        frame = frame.f_back
    return None


def capture_creation_tb() -> str | None:
    """A formatted stack for the current scope construction, when enabled.

    Returns:
        The formatted traceback (minus this helper's own frame) when
        ``CAPTURE_CREATION_TB`` is set, else None.
    """
    if not CAPTURE_CREATION_TB:
        return None
    return "".join(traceback.format_stack()[:-1])
