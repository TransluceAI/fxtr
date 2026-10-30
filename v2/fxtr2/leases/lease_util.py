"""Utility functions for working with leases."""

import contextlib
from typing import Any, AsyncGenerator, Generator

import anyio
import anyio.abc

from fxtr2.leases.lease_types import FreshLifetime, FreshLifetimeSymbol, Lease
from fxtr2.leases.local_lease import UnconditionalLocalLifetime
from fxtr2.util.anyio_util import create_task_group


@contextlib.contextmanager
def lease_cancel_scope(
    lease: Lease[Any], discard_lease: bool = False
) -> Generator[anyio.CancelScope, None, None]:
    """Ensure that the lease is active during a context, cancelling async tasks when it breaks.

    Args:
        lease: The lease to ensure is active during the context.
        discard_lease: Whether to discard the lease after the context exits. If True, the lease will
            be discarded even if the context exits normally. If False, the lease will not be
            discarded and must be discarded manually after the context exits.

    Yields:
        An active CancelScope that will be used to cancel all async tasks when the lease breaks.
    """
    lease.ensure_active()
    lease_broken = None

    try:
        with anyio.CancelScope() as cancel_scope:

            def _cancel_due_to_lease_break(reason: str):
                nonlocal lease_broken
                try:
                    lease.ensure_active()
                except Exception as lease_exc:
                    lease_broken = lease_exc
                else:
                    lease_broken = RuntimeError(
                        f"Internal error: lease {lease} is unexpectedly active after broken"
                        " callback"
                    )
                cancel_scope.cancel(f"Lease {lease} broken ({reason})")

            deregister = lease.register_broken_callback(_cancel_due_to_lease_break)
            try:
                yield cancel_scope
            finally:
                deregister()

    except Exception as inner_exc:
        if lease_broken:
            # This exception was probably downstream of our lease breaking. Report both.
            inner_exc.add_note(
                f"Note: This exception occured while cancelling tasks due to broken lease {lease}."
            )
            raise ExceptionGroup(
                "An additional exception occured while propagating a broken lease exception.",
                [lease_broken, inner_exc],
            ) from None
        # Lease wasn't broken: this is a body-level exception unrelated to the lease.
        # Pass it through unchanged.
        raise

    else:
        if lease_broken:
            raise lease_broken

    finally:
        if discard_lease:
            lease.discard()


@contextlib.asynccontextmanager
async def open_lifetime_task_group[
    # Output-only existential brand: the caller pins `RS` (e.g.
    # `open_lifetime_task_group[Literal["wrapper"]](...)`) to give the returned
    # lifetime a distinct brand. It legitimately appears once — pyright's
    # single-use heuristic doesn't model this fresh-lifetime pattern (see the
    # `FreshLifetime` docstring in `lease_types`).
    RS: FreshLifetimeSymbol  # pyright: ignore[reportInvalidTypeVarUse]
](
    name: str,
    *,
    collapse_exc_group: bool = True,
) -> AsyncGenerator[
    tuple[UnconditionalLocalLifetime[FreshLifetime[RS]], anyio.abc.TaskGroup], None
]:
    """Co-construct an `UnconditionalLocalLifetime` and an `anyio` task group.

    Yields `(lifetime, task_group)`. The task group is opened *outside* the lifetime, so
    it outlives the lifetime — matching the "`_task_group` must outlive `_token`" invariant
    used by `IRHandleLocalImplBase` and friends.

    Exit policy:

    - **Normal block exit** → the lifetime runs its standard async-CM drain (discard its
      root lease, wait for all other holders to release their clones). Then the task
      group exits, waiting on any still-running tasks.
    - **Any exception in the body or in a spawned task** → the lifetime is revoked
      synchronously (breaking any outstanding leases, which surfaces to tasks as
      `LeaseBrokenError`), then the task group unwinds (cancels remaining tasks, waits,
      re-raises as usual).

    Args:
        name: Human-readable name for the lifetime.
        collapse_exc_group: When True (default), a `BaseExceptionGroup` containing
            exactly one exception is unwrapped and the single inner exception is raised
            instead — most task-group failures are single-cause in practice, so this
            yields cleaner error reporting. Set to False to preserve the raw
            `BaseExceptionGroup` shape. See
            `fxtr2.util.anyio_util.create_task_group` for details.

    Typical usage (a handle or dispatcher that needs its own lifetime + task group):

    ```python
    async with open_lifetime_task_group("my-dispatcher") as (lifetime, tg):
        handle = MyHandle(token=lifetime.token, task_group=tg)
        # Hand out clones of lifetime.root_lease to callers that need to reach `handle`.
        ...
    ```
    """
    async with create_task_group(collapse_exc_group=collapse_exc_group) as task_group:
        async with UnconditionalLocalLifetime[FreshLifetime[RS]](name) as lifetime:
            yield lifetime, task_group
