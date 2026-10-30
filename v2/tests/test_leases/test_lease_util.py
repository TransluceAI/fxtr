"""Tests for fxtr2.leases.lease_util — `lease_cancel_scope` and `open_lifetime_task_group`."""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from fxtr2.leases.lease_types import Lease, LeaseBrokenError
from fxtr2.leases.lease_util import lease_cancel_scope, open_lifetime_task_group
from fxtr2.leases.local_lease import UnconditionalLocalLifetime

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# lease_cancel_scope
# ---------------------------------------------------------------------------


class TestLeaseCancelScope:
    async def test_normal_exit(self) -> None:
        """Body runs and exits cleanly; scope was never cancelled."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            with lease_cancel_scope(lease) as scope:
                await anyio.sleep(0)
                assert not scope.cancel_called
            # Lease still active (discard_lease=False default).
            assert lease.is_active()
            lease.discard()

    async def test_discard_lease_on_exit(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            with lease_cancel_scope(lease, discard_lease=True):
                pass
            assert not lease.is_active()

    async def test_inactive_lease_at_entry_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            keep = lt.root_lease.clone()
            dead = keep.clone()
            dead.discard()
            with pytest.raises(Exception):
                with lease_cancel_scope(dead):
                    pass
            keep.discard()

    async def test_lease_break_mid_body_cancels_and_raises(self) -> None:
        """Lease breaking during the body cancels the scope; the resulting
        `LeaseBrokenError` is raised when the scope exits."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            caught: list[BaseException] = []
            async with anyio.create_task_group() as tg:

                async def revoker() -> None:
                    await anyio.sleep(0.01)
                    lt.revoke("break!")

                tg.start_soon(revoker)
                try:
                    with lease_cancel_scope(lease):
                        await anyio.sleep(1.0)  # will be cancelled by lease break
                        raise AssertionError("sleep should have been cancelled")
                except LeaseBrokenError as e:
                    caught.append(e)
            assert len(caught) == 1

    async def test_unrelated_body_exception_propagates(self) -> None:
        """Regression: an exception raised inside the body when the lease has *not*
        broken must propagate out unchanged.

        The original implementation caught any ``Exception`` and only re-raised when
        ``lease_broken`` was set, silently swallowing body exceptions on the
        not-broken path. This was a real bug — without this test, callers (e.g.
        ``open_rpc_scope``) saw their body's ``RuntimeError`` disappear and the
        outer ``async with`` exit normally as though nothing had happened.
        """
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            with pytest.raises(RuntimeError, match="body raised"):
                with lease_cancel_scope(lease):
                    raise RuntimeError("body raised")
            # Lease should still be active — the cancel scope has nothing to do
            # with the body's exception in this case.
            assert lease.is_active()
            lease.discard()

    async def test_unrelated_body_baseexception_propagates(self) -> None:
        """Same regression check, for ``BaseException`` subclasses (e.g. cancellation,
        ``KeyboardInterrupt``, ``SystemExit``). The original ``except Exception`` clause
        wouldn't catch these (so they'd happen to propagate), but they're worth pinning
        down to ensure no future "broaden the catch" change re-introduces the bug."""

        class _MyBaseExc(BaseException):
            pass

        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            with pytest.raises(_MyBaseExc, match="base raised"):
                with lease_cancel_scope(lease):
                    raise _MyBaseExc("base raised")
            assert lease.is_active()
            lease.discard()

    async def test_downstream_exception_reports_both(self) -> None:
        """If the body itself raises *during* cancellation (downstream of the break),
        we get an ExceptionGroup containing both the LeaseBrokenError and the body's
        exception."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            caught_groups: list[ExceptionGroup[Any]] = []
            async with anyio.create_task_group() as tg:

                async def revoker() -> None:
                    await anyio.sleep(0.01)
                    lt.revoke("break!")

                tg.start_soon(revoker)
                try:
                    with lease_cancel_scope(lease):
                        try:
                            await anyio.sleep(1.0)
                        except BaseException:
                            # Synthesize a downstream exception during cleanup.
                            raise RuntimeError("downstream error") from None
                except ExceptionGroup as eg:
                    caught_groups.append(eg)
            assert len(caught_groups) == 1
            exc_types = {type(e) for e in caught_groups[0].exceptions}
            assert LeaseBrokenError in exc_types
            assert RuntimeError in exc_types


# ---------------------------------------------------------------------------
# open_lifetime_task_group
# ---------------------------------------------------------------------------


class TestOpenLifetimeTaskGroup:
    async def test_basic_usage(self) -> None:
        async with open_lifetime_task_group("t") as (lt, tg):
            # Lifetime is alive, task group is ours.
            assert not lt.token.ended()
            ran: list[None] = []

            async def t() -> None:
                ran.append(None)

            tg.start_soon(t)
        # After exit: lifetime ended (drain), all tasks ran.
        assert lt.token.ended()
        assert ran == [None]

    async def test_normal_exit_drains(self) -> None:
        """On normal exit, the lifetime's root lease is discarded and we wait for any
        other holders (spawned tasks) to release their clones."""
        async with open_lifetime_task_group("t") as (lt, tg):
            held = lt.root_lease.clone()

            async def holder() -> None:
                await anyio.sleep(0.02)
                held.discard()

            tg.start_soon(holder)
        # The drain wait should have kept `async with` alive until holder discarded.
        assert lt.token.ended()

    async def test_body_exception_revokes_and_propagates(self) -> None:
        """By default, a single-cause failure comes through as the inner exception
        (collapse_exc_group=True)."""
        captured: list[Any] = []
        with pytest.raises(ValueError, match="body blew up"):
            async with open_lifetime_task_group("t") as (lt, _tg):
                captured.append(lt.token)
                raise ValueError("body blew up")
        assert captured[0].ended()

    async def test_task_exception_revokes_and_propagates(self) -> None:
        captured: list[Any] = []
        with pytest.raises(RuntimeError, match="task blew up"):
            async with open_lifetime_task_group("t") as (lt, tg):
                captured.append(lt.token)

                async def bad() -> None:
                    await anyio.sleep(0.01)
                    raise RuntimeError("task blew up")

                tg.start_soon(bad)
        assert captured[0].ended()

    async def test_collapse_exc_group_false_preserves_group(self) -> None:
        """Opting out of collapse leaves the `BaseExceptionGroup` intact."""
        with pytest.raises(BaseExceptionGroup) as excinfo:
            async with open_lifetime_task_group("t", collapse_exc_group=False) as (_lt, _tg):
                raise ValueError("body blew up")
        assert any(isinstance(e, ValueError) for e in excinfo.value.exceptions)

    async def test_task_group_outlives_lifetime(self) -> None:
        """A task that waits on the lifetime's token-end (via a lease-break) can still
        complete after the lifetime is revoked, because the task group is the *outer*
        context manager and stays open until after the lifetime exits."""
        observed: list[str] = []

        async def revoke_observer(lease: Lease[Any]) -> None:
            # Wait for the lease to break, then record.
            try:
                while lease.is_active():
                    await anyio.sleep(0.005)
            except BaseException:
                pass
            observed.append("saw-break")

        async with open_lifetime_task_group("t") as (lt, tg):
            tg.start_soon(revoke_observer, lt.root_lease.clone())
            lt.revoke("test")
        assert observed == ["saw-break"]
