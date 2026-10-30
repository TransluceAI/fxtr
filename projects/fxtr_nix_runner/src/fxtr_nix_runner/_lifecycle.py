"""``RefcountedResourceRegistry`` — process-global, refcounted async resources.

The podman backend keeps **one builder container per git-repo-root per
Python process**, shared across every in-process consumer and torn down
when the last user goes away (structured-concurrency style — no idle
container sprawl across runs). The deliverer follows the same shape. Both
are instances of this generic registry, which is podman-free and
unit-testable on its own.

Contract:

- ``borrow(key)`` is an async CM. The first borrower of a ``key`` runs
  ``factory(key)`` to build the resource; concurrent/later borrowers share
  it. When the last borrower's CM exits, ``teardown(resource)`` runs.
- ``pin(key)`` is an async CM that keeps a key's resource *warm* without
  itself instantiating it: it never runs ``factory``, but while a pin is held
  the resource is **not** torn down when the borrow count hits 0. So the first
  ``borrow`` under a pin builds the resource, borrow cycles between then and
  the pin's release reuse it, and ``teardown`` runs only once **both** the
  borrow count and the pin count reach 0. (The podman backend uses this for a
  builder kept warm across a spawn environment's spawns.)
- ``factory`` / ``teardown`` are awaited **outside** the bookkeeping lock,
  so creating/destroying one key's resource (a slow container start/stop)
  never blocks borrows of other keys.
- ``teardown`` is **shielded** against cancellation (bounded by
  ``teardown_timeout``): a borrow cancelled mid-flight still tears its
  resource down to completion rather than orphaning it.
- If ``factory`` raises, the error propagates to that borrower and no
  half-built resource is shared; the entry is dropped unless a pin is holding
  it, in which case the next borrow retries the factory.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Generic, TypeVar

import anyio

logger = logging.getLogger(__name__)

K = TypeVar("K")
R = TypeVar("R")


@dataclass
class _Entry(Generic[R]):
    refcount: int = 0  # active borrows
    pincount: int = 0  # active pins (keepalive; never instantiate)
    resource: R | None = None
    building: bool = False  # a creator is currently running factory()
    # Set once the current build attempt finishes (success or failure). Waiters
    # that joined during a build block on it so they never observe a half-built
    # entry; it's replaced with a fresh event per build attempt.
    ready: anyio.Event = field(default_factory=anyio.Event)
    failed: bool = False


class RefcountedResourceRegistry(Generic[K, R]):
    """A keyed pool of async resources shared by reference count."""

    def __init__(
        self,
        factory: Callable[[K], Awaitable[R]],
        teardown: Callable[[R], Awaitable[None]],
        *,
        teardown_timeout: float = 60.0,
    ) -> None:
        """
        Args:
            factory: ``async (key) -> resource`` — builds the resource for a
                key. Called exactly once per key while no resource for it
                exists (the first borrower; later/concurrent borrowers reuse
                the result). If it raises, the failed entry is dropped and the
                error propagates to that borrower.
            teardown: ``async (resource) -> None`` — disposes a resource when
                its last borrower's context exits. Awaited outside the
                bookkeeping lock, and **shielded** so it runs to completion even
                when the borrowing scope is being cancelled (a cancelled borrow
                must still tear its resource down — e.g. ``podman rm`` a builder
                — rather than orphan it).
            teardown_timeout: Upper bound (seconds) on the shielded teardown
                await, so a hung teardown can't deadlock the release. Default
                60s; ``podman rm -f`` is normally sub-second.
        """
        self._factory = factory
        self._teardown = teardown
        self._teardown_timeout = teardown_timeout
        self._entries: dict[K, _Entry[R]] = {}
        # Guards only the dict bookkeeping — never held across factory /
        # teardown awaits.
        self._guard = anyio.Lock()

    @asynccontextmanager
    async def borrow(self, key: K) -> AsyncIterator[R]:
        """Borrow the (shared, refcounted) resource for ``key``.

        The first borrower of a key runs ``factory(key)``; concurrent and
        subsequent borrowers receive the same resource. ``teardown`` runs when
        the last live borrow's context exits — so the resource lives exactly as
        long as at least one borrower holds it.

        Args:
            key: The resource key (e.g. a repo root). Must be hashable.

        Yields:
            The resource for ``key`` (shared with any concurrent borrowers).
        """
        resource = await self._acquire(key)
        try:
            yield resource
        finally:
            await self._decref(key, borrow=True)

    @asynccontextmanager
    async def pin(self, key: K) -> AsyncIterator[None]:
        """Keep ``key``'s resource warm for the block **without instantiating
        it**.

        Entering does not run ``factory`` — it only registers intent, so a key
        that is pinned but never borrowed stays absent. The first ``borrow``
        under the pin builds the resource; while the pin is held it is not torn
        down when the borrow count returns to 0; ``teardown`` runs when both the
        borrow count and pin count reach 0 (either the last borrow exiting with
        no pins, or this pin exiting with no borrows). Reentrant: nested pins of
        a key share, released last-out.

        Args:
            key: The resource key (same key space as :meth:`borrow`).
        """
        async with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry()
                self._entries[key] = entry
            entry.pincount += 1
        try:
            yield
        finally:
            await self._decref(key, borrow=False)

    async def _acquire(self, key: K) -> R:
        # Loop (rather than recurse) so a borrower that joined a build attempt
        # which then failed simply retries.
        while True:
            async with self._guard:
                entry = self._entries.get(key)
                if entry is None:
                    entry = _Entry()
                    self._entries[key] = entry
                entry.refcount += 1
                if entry.resource is not None:
                    return entry.resource  # already built — fast path
                if entry.building:
                    creator = False
                    ready = entry.ready
                else:
                    # First borrow of an unbuilt entry (fresh, or one that a pin
                    # created / a prior build left failed) — become the creator.
                    entry.building = True
                    entry.failed = False
                    entry.ready = anyio.Event()
                    ready = entry.ready
                    creator = True

            if creator:
                try:
                    resource = await self._factory(key)
                except BaseException:
                    # Undo our borrow; wake waiters. Keep the entry iff a pin
                    # still holds it (next borrow retries the factory), else drop.
                    async with self._guard:
                        entry.building = False
                        entry.failed = True
                        entry.refcount -= 1
                        self._drop_if_idle(key, entry)
                    ready.set()
                    raise
                async with self._guard:
                    entry.resource = resource
                    entry.building = False
                ready.set()
                return resource

            # Waiter: block on the in-flight build, then re-check.
            await ready.wait()
            async with self._guard:
                if entry.resource is not None:
                    return entry.resource
                # The build we waited on failed — undo our borrow and retry.
                entry.refcount -= 1
                self._drop_if_idle(key, entry)

    def _drop_if_idle(self, key: K, entry: _Entry[R]) -> None:
        """Remove an entry that nothing references and isn't building. Call under
        the guard. Never tears down a resource (used on the no-resource paths)."""
        if (
            entry.refcount == 0
            and entry.pincount == 0
            and not entry.building
            and self._entries.get(key) is entry
        ):
            self._entries.pop(key, None)

    async def _decref(self, key: K, *, borrow: bool) -> None:
        # Shield the whole release: this runs from a `finally`, which may be
        # unwinding a *cancelled* scope — without the shield the bookkeeping lock
        # or the teardown await would themselves be cancelled, leaking the count
        # or orphaning the resource (e.g. an un-removed builder container). The
        # teardown is additionally bounded by `teardown_timeout` so a hung
        # teardown can't deadlock the release.
        with anyio.CancelScope(shield=True):
            async with self._guard:
                entry = self._entries.get(key)
                if entry is None:
                    return
                if borrow:
                    entry.refcount -= 1
                else:
                    entry.pincount -= 1
                # Keep the resource warm while any borrow or pin still holds it
                # (or a build is in flight — a creator holds a borrow anyway).
                if entry.refcount > 0 or entry.pincount > 0 or entry.building:
                    return
                # Nothing wants it anymore — remove now (under the guard) so a
                # concurrent borrow starts a fresh resource rather than racing
                # teardown.
                self._entries.pop(key, None)
                resource = entry.resource

            if resource is not None:
                await self._teardown_bounded(key, resource)

    async def invalidate(self, key: K, resource: R) -> None:
        """Drop ``resource`` from ``key``'s entry because it's believed dead, so
        the next :meth:`borrow` rebuilds it.

        Keeps the entry (and any pin), so a pinned-warm resource re-warms on the
        next borrow rather than the pin being lost. Identity-checked: a no-op if
        the cached resource is no longer ``resource`` (already recycled by a
        concurrent caller, or replaced). The dead resource is still handed to
        ``teardown`` (best-effort — e.g. ``podman rm -f`` of a gone container
        just no-ops), so no bookkeeping is orphaned.

        Setting the cached resource to ``None`` (rather than popping the entry)
        is what makes this safe while a borrow is still live: that borrow's
        release reads ``entry.resource`` as ``None`` and tears nothing down, and
        the next borrow sees "not built" and rebuilds. Used by the podman backend
        to recycle a warm builder whose container died mid-env.

        Args:
            key: The resource key.
            resource: The specific resource instance believed dead. Only that
                instance is invalidated (identity comparison).
        """
        with anyio.CancelScope(shield=True):
            async with self._guard:
                entry = self._entries.get(key)
                if entry is None or entry.resource is not resource:
                    return  # already recycled / replaced / gone
                entry.resource = None
            await self._teardown_bounded(key, resource)

    async def _teardown_bounded(self, key: K, resource: R) -> None:
        """Run ``teardown(resource)`` bounded by ``teardown_timeout``, warning on
        timeout. Call inside a shielded scope (callers already are)."""
        with anyio.move_on_after(self._teardown_timeout) as cs:
            await self._teardown(resource)
        if cs.cancel_called:
            logger.warning(
                "Teardown of %r timed out after %s seconds",
                key,
                self._teardown_timeout,
            )

    def active_keys(self) -> list[K]:
        """Keys with a live entry (built, building, or pin-reserved). For
        tests/debug."""
        return list(self._entries.keys())
