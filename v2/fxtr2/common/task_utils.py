"""Task-start helpers that install a controlled scope context.

Every ``tg.start`` / ``tg.start_soon`` copies the calling task's
context — including the active-scope contextvar — so a task started
from a context you don't control (above all: a service dispatch)
silently inherits an ambient scope with no relationship to its own
lifetime (see the "Avoid Capturing Ambient Scope Context In Tasks"
section of ``v2/notes/scope_discipline.md``). These helpers package
the sanctioned response: start the task with an explicitly chosen
active scope installed at task top, or — with ``scope=None`` — with
*no* active scope, so any implicit use anywhere in the task fails
immediately with ``NoActiveScopeError`` instead of silently binding
to the inherited snapshot.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Awaitable, Callable

import anyio.abc

from ..data.entity_ref_scope import FxtrScope, no_active_scope

__all__ = ["start_soon_with_scope", "start_with_scope"]


def _installed_context(scope: FxtrScope | None) -> AbstractContextManager[None]:
    return scope.as_active_scope() if scope is not None else no_active_scope()


def start_soon_with_scope(
    tg: anyio.abc.TaskGroup,
    fn: Callable[..., Awaitable[object]],
    /,
    *args: object,
    scope: FxtrScope | None,
    name: object = None,
) -> None:
    """``tg.start_soon``, with a chosen scope context installed at task top.

    Args:
        tg: The task group to start the task on.
        fn: The async function to run (``await fn(*args)``).
        *args: Positional arguments for ``fn``.
        scope: The scope to make active for the task's whole body — or
            ``None`` to run the task with *no* active scope (implicit
            active-scope use raises ``NoActiveScopeError``). Required:
            choosing is the point.
        name: Task name for debugging; defaults to ``fn``'s qualname.
    """

    async def _runner() -> None:
        with _installed_context(scope):
            await fn(*args)

    tg.start_soon(_runner, name=name or getattr(fn, "__qualname__", repr(fn)))


async def start_with_scope(
    tg: anyio.abc.TaskGroup,
    fn: Callable[..., Awaitable[object]],
    /,
    *args: object,
    scope: FxtrScope | None,
    name: object = None,
) -> Any:
    """``await tg.start``, with a chosen scope context installed at task top.

    Args:
        tg: The task group to start the task on.
        fn: The async function to run; called as
            ``await fn(*args, task_status=task_status)`` and expected to
            call ``task_status.started(...)`` like any ``tg.start``
            target.
        *args: Positional arguments for ``fn``.
        scope: The scope to make active for the task's whole body — or
            ``None`` to run the task with *no* active scope (implicit
            active-scope use raises ``NoActiveScopeError``). Required:
            choosing is the point.
        name: Task name for debugging; defaults to ``fn``'s qualname.

    Returns:
        Whatever the task passed to ``task_status.started(...)``.
    """

    async def _runner(*, task_status: anyio.abc.TaskStatus[Any]) -> None:
        with _installed_context(scope):
            await fn(*args, task_status=task_status)

    return await tg.start(_runner, name=name or getattr(fn, "__qualname__", repr(fn)))
