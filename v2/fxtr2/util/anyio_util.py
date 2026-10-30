"""Utilities for working with anyio in fxtr."""

from __future__ import annotations

import contextlib
import logging
from typing import AsyncIterator

import anyio
import anyio.abc

logger = logging.getLogger(__name__)


class BaseExceptionDetachedGroup(BaseException):
    """A base exception used as a placeholder for a base exception group that was collapsed."""


class ExceptionDetachedGroup(Exception):
    """An exception used as a placeholder for an exception group that was collapsed."""


@contextlib.asynccontextmanager
async def create_task_group(
    *, collapse_exc_group: bool = False
) -> AsyncIterator[anyio.abc.TaskGroup]:
    """Create a task group, with convenience options for collapsing exception groups.

    Args:
        collapse_exc_group: Whether to collapse exception groups into a single exception. If set,
            if an exception happens inside the task group but there is _exactly one_ exception in
            the group, the exception group will be removed and replaced by the single exception it
            contains. This is useful for simplifying error reporting in cases where there are many
            nested task groups but most of them do not actually contribute to the error. If enabled,
            the original info about the exception will be instead logged at DEBUG level.

    Returns:
        A task group.
    """
    should_raise: BaseException | None = None
    try:
        async with anyio.create_task_group() as tg:
            yield tg
    except BaseExceptionGroup as grp:
        if (
            collapse_exc_group
            and len(grp.exceptions) == 1
            and grp.message == "unhandled errors in a TaskGroup"
        ):

            # Save the exception to re-raise it alone. We explicitly delay this to happen outside
            # the try-except block so that the original context is preserved, because otherwise the
            # __context__ attribute would be set to the BaseExceptionGroup instead.
            should_raise = grp.exceptions[0]

            if logger.isEnabledFor(logging.DEBUG):
                # Create a detached exception to represent the collapsed exception group.
                if isinstance(grp, ExceptionGroup):
                    decoupled_exc = ExceptionDetachedGroup(grp.message)
                else:
                    decoupled_exc = BaseExceptionDetachedGroup(grp.message)

                # Copy over all other attributes of the original exception group that would be
                # lost when the exception group is collapsed.
                decoupled_exc.__traceback__ = grp.__traceback__
                decoupled_exc.__cause__ = grp.__cause__
                decoupled_exc.__context__ = grp.__context__
                notes = getattr(grp, "__notes__", None)
                if notes is not None:
                    decoupled_exc.__notes__ = notes

                # Log the collapsed exception to aid debugging.
                logger.debug(
                    "Collapsing a singleton exception group (previously contained: %s)",
                    repr(should_raise),
                    exc_info=decoupled_exc,
                )

        else:
            raise

    # Reraise the child exception alone outside the except block.
    if should_raise is not None:
        raise should_raise
