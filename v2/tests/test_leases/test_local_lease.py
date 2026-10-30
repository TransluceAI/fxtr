"""Tests for fxtr2.leases.local_lease — local lifetime primitives.

Covers `LocalLifetimeToken`, `LocalGrant`, `LocalLifetimeLease`,
`UnconditionalLocalLifetime`, `LocalDerivedTokenLease`, and `DerivedLocalLifetime`.

`LocalGrant` is exercised as a pure identity object. The lease-tracking behavior lives on the
lifetime owner (`UnconditionalLocalLifetime` / `DerivedLocalLifetime`) and is covered through
that public API.
"""

from __future__ import annotations

from typing import Any

import pytest

from fxtr2.leases.lease_types import Lease, LeaseBrokenError, LeaseDiscardedError
from fxtr2.leases.local_lease import (
    DerivedLocalLifetime,
    LocalDerivedTokenLease,
    LocalGrant,
    LocalLifetimeToken,
    UnconditionalLocalLifetime,
    _DerivedOutputGrant,
)

# ---------------------------------------------------------------------------
# LocalLifetimeToken
# ---------------------------------------------------------------------------


class TestLocalLifetimeToken:
    def test_new_token_is_active(self) -> None:
        token = LocalLifetimeToken("t")
        assert not token.ended()

    def test_mark_ended_fires_callbacks_in_registration_order(self) -> None:
        token = LocalLifetimeToken("t")
        fired: list[str] = []
        token.register_end_callback(lambda: fired.append("a"))
        token.register_end_callback(lambda: fired.append("b"))
        token.register_end_callback(lambda: fired.append("c"))
        token._mark_ended()
        assert fired == ["a", "b", "c"]
        assert token.ended()

    def test_mark_ended_is_idempotent(self) -> None:
        token = LocalLifetimeToken("t")
        fired: list[None] = []
        token.register_end_callback(lambda: fired.append(None))
        token._mark_ended()
        token._mark_ended()
        assert len(fired) == 1

    def test_callback_on_already_ended_fires_immediately(self) -> None:
        token = LocalLifetimeToken("t")
        token._mark_ended()
        fired: list[None] = []
        deregister = token.register_end_callback(lambda: fired.append(None))
        assert fired == [None]
        # Deregister is a no-op in this case but must be callable.
        deregister()

    def test_deregister_prevents_callback(self) -> None:
        token = LocalLifetimeToken("t")
        fired: list[None] = []
        deregister = token.register_end_callback(lambda: fired.append(None))
        deregister()
        token._mark_ended()
        assert fired == []

    def test_double_deregister_is_safe(self) -> None:
        token = LocalLifetimeToken("t")
        deregister = token.register_end_callback(lambda: None)
        deregister()
        deregister()  # must not raise

    def test_exception_in_callback_does_not_stop_others(self) -> None:
        token = LocalLifetimeToken("t")
        fired: list[str] = []

        def bad() -> None:
            raise RuntimeError("boom")

        token.register_end_callback(lambda: fired.append("before"))
        token.register_end_callback(bad)
        token.register_end_callback(lambda: fired.append("after"))
        token._mark_ended()
        assert fired == ["before", "after"]


class TestHashability:
    """Tokens and grants are usable as dict keys. Exercised so that future subclasses
    that accidentally disable hashing (e.g. by defining ``__eq__`` without
    ``__hash__``) break this test rather than silently breaking the RPC re-export
    checker that relies on dict-keying by token/grant identity."""

    def test_local_token_is_dict_key(self) -> None:
        t1 = LocalLifetimeToken("t1")
        t2 = LocalLifetimeToken("t2")
        d: dict[LocalLifetimeToken[Any], int] = {t1: 1, t2: 2}
        assert d[t1] == 1
        assert d[t2] == 2
        # Identity-hashing: two tokens with the same name are still distinct keys.
        t1_again = LocalLifetimeToken("t1")
        assert t1_again not in d

    def test_local_grant_is_dict_key(self) -> None:
        t = LocalLifetimeToken("t")
        g1 = LocalGrant(t)
        g2 = LocalGrant(t)  # identity-distinct even with the same subject
        d: dict[LocalGrant[Any], int] = {g1: 1, g2: 2}
        assert d[g1] == 1
        assert d[g2] == 2

    def test_derived_output_grant_is_dict_key(self) -> None:
        # Frozen dataclass → value-based equality + hash. Two grants on the same
        # (derived, frozenset-of-input-grants) pair are equal and share a dict slot.
        with (
            UnconditionalLocalLifetime("input").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            out1 = der.exchange_forward({inp.token: inp.root_lease.clone()})
            out2 = der.exchange_forward({inp.token: inp.root_lease.clone()})
            d: dict[Any, int] = {out1.grant: 1}
            # Same-input grant → equal output grants → overwrites.
            d[out2.grant] = 2
            assert d[out1.grant] == 2
            out1.discard()
            out2.discard()


# ---------------------------------------------------------------------------
# LocalGrant (pure identity)
# ---------------------------------------------------------------------------


class TestLocalGrant:
    def test_subject_returns_underlying_token(self) -> None:
        token = LocalLifetimeToken("t")
        grant = LocalGrant(token)
        assert grant.subject is token

    def test_identity_based_equality(self) -> None:
        token = LocalLifetimeToken("t")
        g1 = LocalGrant(token)
        g2 = LocalGrant(token)
        # Distinct instances over the same token are distinct grants.
        assert g1 == g1
        assert g1 != g2

    def test_hashable(self) -> None:
        token = LocalLifetimeToken("t")
        grant = LocalGrant(token)
        # Usable as a dict key / set member.
        assert hash(grant) == hash(grant)
        assert {grant: "value"}[grant] == "value"
        assert grant in {grant}


# ---------------------------------------------------------------------------
# LocalLifetimeLease (exercised via UnconditionalLocalLifetime)
# ---------------------------------------------------------------------------


class TestLocalLifetimeLease:
    def test_clone_shares_grant(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            l1 = lt.root_lease
            l2 = l1.clone()
            assert l1.grant is l2.grant
            assert l1 is not l2
            assert l1.is_active() and l2.is_active()

    def test_take_returns_fresh_and_discards_original(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            l1 = lt.root_lease.clone()
            l2 = l1.take()
            assert not l1.is_active()
            assert l2.is_active()
            assert l1.grant is l2.grant

    def test_clone_raises_on_broken(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        lease = lt.root_lease.clone()
        lt.revoke("bye")
        with pytest.raises(LeaseBrokenError):
            lease.clone()

    def test_clone_raises_on_discarded(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            keepalive = lt.root_lease.clone()  # keep lifetime alive past the discard below
            lease = keepalive.clone()
            lease.discard()
            with pytest.raises(LeaseDiscardedError):
                lease.clone()

    def test_discard_is_idempotent(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            keepalive = lt.root_lease.clone()
            lease = keepalive.clone()
            lease.discard()
            lease.discard()  # no-op, no exception
            assert not lease.is_active()

    def test_ensure_active_raises_after_break(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        lease = lt.root_lease.clone()
        lt.revoke("bye")
        with pytest.raises(LeaseBrokenError):
            lease.ensure_active()

    def test_ensure_active_raises_after_discard(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            keepalive = lt.root_lease.clone()
            lease = keepalive.clone()
            lease.discard()
            with pytest.raises(LeaseDiscardedError):
                lease.ensure_active()

    def test_register_broken_on_live_lease_fires_on_break(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        lease = lt.root_lease.clone()
        reasons: list[str] = []
        lease.register_broken_callback(lambda r: reasons.append(r))
        lt.revoke("bye")
        assert reasons == ["bye"]

    def test_register_broken_on_already_broken_fires_immediately(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        lease = lt.root_lease.clone()
        lt.revoke("bye")
        reasons: list[str] = []
        deregister = lease.register_broken_callback(lambda r: reasons.append(r))
        assert reasons == ["bye"]
        deregister()  # must be a callable no-op

    def test_register_broken_on_discarded_is_noop(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            keepalive = lt.root_lease.clone()
            lease = keepalive.clone()
            lease.discard()
            reasons: list[str] = []
            deregister = lease.register_broken_callback(lambda r: reasons.append(r))
            assert reasons == []
            deregister()

    def test_deregister_broken_callback(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        lease = lt.root_lease.clone()
        reasons: list[str] = []
        deregister = lease.register_broken_callback(lambda r: reasons.append(r))
        deregister()
        lt.revoke("bye")
        assert reasons == []

    def test_context_manager_discards_on_exit(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            keepalive = lt.root_lease.clone()
            lease = keepalive.clone()
            with lease as inner:
                assert inner is lease
                assert lease.is_active()
            assert not lease.is_active()

    def test_context_manager_on_inactive_lease_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            keepalive = lt.root_lease.clone()
            lease = keepalive.clone()
            lease.discard()
            with pytest.raises(LeaseDiscardedError):
                with lease:
                    pass

    def test_clone_then_discard_original_keeps_clone_alive(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            clone = lease.clone()
            lease.discard()
            assert clone.is_active()
            clone.ensure_active()  # must not raise


# ---------------------------------------------------------------------------
# UnconditionalLocalLifetime
# ---------------------------------------------------------------------------


class TestUnconditionalLocalLifetime:
    def test_token_is_active_on_construction(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            assert not lt.token.ended()

    def test_root_lease_is_alive_immediately(self) -> None:
        """Root lease is eagerly constructed at __init__ time."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            assert lt.root_lease.is_active()
            assert lt.root_lease.subject is lt.token

    def test_root_lease_is_stable_reference(self) -> None:
        """Accessing `root_lease` repeatedly returns the exact same lease object."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            assert lt.root_lease is lt.root_lease

    def test_unaccessed_lifetime_stays_open(self) -> None:
        """Creating a lifetime without doing anything to it leaves the token alive."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            # The internal root lease keeps the token alive until someone discards it.
            assert not lt.token.ended()

    def test_all_leases_share_one_grant(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            root = lt.root_lease
            c1 = root.clone()
            c2 = c1.clone()
            assert root.grant is c1.grant is c2.grant

    def test_auto_close_on_last_lease_discarded(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        root = lt.root_lease
        c1 = root.clone()
        fired: list[None] = []
        lt.token.register_end_callback(lambda: fired.append(None))
        c1.discard()
        assert not lt.token.ended()
        assert fired == []
        root.discard()
        assert lt.token.ended()
        assert fired == [None]

    def test_discard_root_alone_closes_lifetime(self) -> None:
        """If no clones exist, discarding the root is enough to close the lifetime."""
        lt = UnconditionalLocalLifetime("lt")
        lt.root_lease.discard()
        assert lt.token.ended()

    def test_revoke_breaks_all_leases_and_ends_token(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        root = lt.root_lease
        c1 = root.clone()
        c2 = c1.clone()
        reasons: list[str] = []
        for lease in (root, c1, c2):
            lease.register_broken_callback(lambda r: reasons.append(r))
        ended: list[None] = []
        lt.token.register_end_callback(lambda: ended.append(None))
        lt.revoke("bye")
        assert reasons == ["bye", "bye", "bye"]
        assert ended == [None]
        assert lt.token.ended()
        for lease in (root, c1, c2):
            assert not lease.is_active()
            with pytest.raises(LeaseBrokenError):
                lease.ensure_active()

    def test_revoke_fires_end_callbacks_exactly_once(self) -> None:
        """Revoke must not double-fire end-callbacks via an auto-close side channel."""
        lt = UnconditionalLocalLifetime("lt")
        ended: list[None] = []
        lt.token.register_end_callback(lambda: ended.append(None))
        lt.revoke("bye")
        assert ended == [None]

    def test_double_revoke_is_idempotent(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        lt.revoke("first")
        lt.revoke("second")  # no exception
        assert lt.token.ended()

    def test_revoke_after_natural_close_is_idempotent(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        lt.root_lease.discard()  # natural close
        assert lt.token.ended()
        lt.revoke("post-close")  # no-op, no raise
        lt.revoke("again")  # still no raise
        assert lt.token.ended()

    def test_root_lease_after_revoke_returns_broken_lease(self) -> None:
        """`root_lease` is a stable reference; after revoke the lease is broken, not absent."""
        lt = UnconditionalLocalLifetime("lt")
        lt.revoke("bye")
        lease = lt.root_lease
        assert not lease.is_active()
        with pytest.raises(LeaseBrokenError):
            lease.ensure_active()

    def test_root_lease_after_natural_close_returns_discarded_lease(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        lt.root_lease.discard()
        lease = lt.root_lease
        assert not lease.is_active()
        with pytest.raises(LeaseDiscardedError):
            lease.ensure_active()

    def test_clone_after_revoke_raises(self) -> None:
        """Even before the holder has noticed, cloning from a revoked lease must fail."""
        lt = UnconditionalLocalLifetime("lt")
        lease = lt.root_lease.clone()
        lt.revoke("bye")
        with pytest.raises(LeaseBrokenError):
            lease.clone()

    def test_take_pattern_preserves_lifetime(self) -> None:
        """The documented take-based callee pattern: caller clones when passing, callee takes."""
        lt = UnconditionalLocalLifetime("lt")
        caller_lease = lt.root_lease.clone()

        def callee(lease: Lease[Any]) -> None:
            with lease.take() as owned:
                assert owned.is_active()
                # Clone for further sub-calls if needed.
                owned.clone().discard()
            # `owned` is discarded here.

        callee(caller_lease.clone())
        # Caller's own lease is still valid.
        assert caller_lease.is_active()
        caller_lease.discard()
        # Root still alive — user must discard it too.
        assert not lt.token.ended()
        lt.root_lease.discard()
        assert lt.token.ended()


# ---------------------------------------------------------------------------
# UnconditionalLocalLifetime.aclose
# ---------------------------------------------------------------------------


_TEST_DEADLOCK_TIMEOUT: float = 5.0


class TestUnconditionalAclose:
    @pytest.mark.anyio
    async def test_aclose_discards_root_and_waits_for_drain(self) -> None:
        import anyio

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            lt = UnconditionalLocalLifetime("lt")
            clone = lt.root_lease.clone()

            finished = False

            async def closer() -> None:
                nonlocal finished
                await lt.aclose()
                finished = True

            async with anyio.create_task_group() as tg:
                tg.start_soon(closer)
                # Let the closer run up to the wait; the token is not yet ended because
                # `clone` is still held.
                await anyio.sleep(0.01)
                assert not finished
                assert not lt.token.ended()
                clone.discard()  # last holder gone → drain completes
            # Task group awaited; closer must have returned.
            assert finished
            assert lt.token.ended()

    @pytest.mark.anyio
    async def test_aclose_revokes_on_cancellation(self) -> None:
        import anyio

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            lt = UnconditionalLocalLifetime("lt")
            keep = lt.root_lease.clone()

            # Cancel the task group that's running aclose; its inner wait should surface
            # the cancellation, aclose should revoke, and the task group should exit.
            async with anyio.create_task_group() as tg:
                tg.start_soon(lt.aclose)
                await anyio.sleep(0.01)
                tg.cancel_scope.cancel()
            assert lt.token.ended()
            assert not keep.is_active()
            keep.discard()

    @pytest.mark.anyio
    async def test_aclose_is_idempotent_on_already_ended(self) -> None:
        import anyio

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            lt = UnconditionalLocalLifetime("lt")
            lt.root_lease.discard()  # natural close
            assert lt.token.ended()
            # Second and third calls must not raise.
            await lt.aclose()
            await lt.aclose()

    @pytest.mark.anyio
    async def test_async_cm_calls_aclose(self) -> None:
        import anyio

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            async with UnconditionalLocalLifetime("lt") as lt:
                clone = lt.root_lease.clone()

                async def release_soon() -> None:
                    await anyio.sleep(0.01)
                    clone.discard()

                async with anyio.create_task_group() as tg:
                    tg.start_soon(release_soon)
                # On block exit: aclose discards root + drains.
            assert lt.token.ended()

    @pytest.mark.anyio
    async def test_async_cm_revokes_on_exception(self) -> None:
        import anyio

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            lt = UnconditionalLocalLifetime("lt")
            clone = lt.root_lease.clone()
            with pytest.raises(RuntimeError, match="boom"):
                async with lt:
                    raise RuntimeError("boom")
            assert lt.token.ended()
            assert not clone.is_active()
            clone.discard()


# ---------------------------------------------------------------------------
# _DerivedOutputGrant (frozen dataclass, value-equal + hashable)
# ---------------------------------------------------------------------------


class TestDerivedOutputGrant:
    def test_equal_for_same_derived_and_input_grants(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime[Any]("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            grants = frozenset({inp.root_lease.grant})
            g1 = _DerivedOutputGrant(der, grants)
            g2 = _DerivedOutputGrant(der, grants)
            assert g1 == g2
            assert hash(g1) == hash(g2)

    def test_equal_despite_input_grant_iteration_order(self) -> None:
        # frozenset-based equality means input-order at construction of the grant
        # tuple doesn't matter; two forward-exchange calls with equivalent input
        # leases produce equal output grants regardless of mapping iteration order.
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime[Any]("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            o1 = der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            )
            # Explicitly swap iteration order by using a different insertion order.
            o2 = der.exchange_forward(
                {b.token: b.root_lease.clone(), a.token: a.root_lease.clone()}
            )
            assert o1.grant == o2.grant
            o1.discard()
            o2.discard()

    def test_not_equal_when_input_grants_differ(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime[Any]("der", inputs={a.token}).revoke_after_block() as der,
        ):
            g1 = _DerivedOutputGrant(der, frozenset({a.root_lease.grant}))
            g2 = _DerivedOutputGrant(der, frozenset({b.root_lease.grant}))
            assert g1 != g2

    def test_not_equal_when_derived_differs(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime[Any]("der1", inputs={inp.token}).revoke_after_block() as der1,
            DerivedLocalLifetime[Any]("der2", inputs={inp.token}).revoke_after_block() as der2,
        ):
            grants = frozenset({inp.root_lease.grant})
            g1 = _DerivedOutputGrant(der1, grants)
            g2 = _DerivedOutputGrant(der2, grants)
            assert g1 != g2

    def test_subject_is_output_token(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime[Any]("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            g = _DerivedOutputGrant(der, frozenset({inp.root_lease.grant}))
            assert g.subject is der.token


# ---------------------------------------------------------------------------
# DerivedLocalLifetime — construction + basics
# ---------------------------------------------------------------------------


class TestDerivedBasics:
    def test_output_token_active_on_construction(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            assert not der.token.ended()
            assert der.token is not inp.token

    def test_empty_input_set_is_legal_and_idle(self) -> None:
        with DerivedLocalLifetime("der", inputs=set()).revoke_after_block() as der:
            # No inputs: output is alive forever except via revoke.
            assert not der.token.ended()
            out = der.exchange_forward({})
            assert out.is_active()
            out.discard()
            assert not der.token.ended()

    def test_exchange_forward_produces_active_output_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward({inp.token: inp.root_lease.clone()})
            assert out.is_active()
            assert out.subject is der.token
            assert isinstance(out, LocalDerivedTokenLease)
            out.discard()

    def test_forward_takes_ownership_of_all_inputs(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            la = a.root_lease.clone()
            lb = b.root_lease.clone()
            out = der.exchange_forward({a.token: la, b.token: lb})
            assert not la.is_active()
            assert not lb.is_active()
            out.discard()

    def test_forward_rejects_missing_input_key(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            with pytest.raises(RuntimeError, match="input token set mismatch"):
                der.exchange_forward({a.token: a.root_lease.clone()})

    def test_forward_rejects_extra_input_key(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token}).revoke_after_block() as der,
        ):
            with pytest.raises(RuntimeError, match="input token set mismatch"):
                der.exchange_forward({a.token: a.root_lease.clone(), b.token: b.root_lease.clone()})

    def test_forward_rejects_discarded_input_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            dead = inp.root_lease.clone()
            dead.discard()
            with pytest.raises(LeaseDiscardedError):
                der.exchange_forward({inp.token: dead})

    def test_cloned_output_clones_all_vias(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            o = der.exchange_forward({a.token: a.root_lease.clone(), b.token: b.root_lease.clone()})
            c = o.clone()
            assert o.grant is c.grant
            assert c.is_active()
            # Independent discard: dropping the clone leaves the original alive.
            c.discard()
            assert o.is_active()
            o.discard()

    def test_single_input_acts_like_lifetime_mirror(self) -> None:
        """A Derived with a single input behaves like a pass-through whose output
        ends iff the single input ends."""
        inp = UnconditionalLocalLifetime("inp")
        with DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der:
            out = der.exchange_forward({inp.token: inp.root_lease.clone()})
            assert out.is_active()
            reasons: list[str] = []
            out.register_broken_callback(lambda r: reasons.append(r))
            inp.revoke("bye")
            assert len(reasons) == 1
            assert "ended" in reasons[0]
            assert der.token.ended()

    def test_multi_input_forward_succeeds(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            UnconditionalLocalLifetime("c").revoke_after_block() as c,
            DerivedLocalLifetime(
                "der", inputs={a.token, b.token, c.token}
            ).revoke_after_block() as der,
        ):
            out = der.exchange_forward(
                {
                    a.token: a.root_lease.clone(),
                    b.token: b.root_lease.clone(),
                    c.token: c.root_lease.clone(),
                }
            )
            assert out.is_active()
            out.discard()


# ---------------------------------------------------------------------------
# exchange_reverse
# ---------------------------------------------------------------------------


class TestDerivedExchangeReverse:
    def test_reverse_returns_dict_of_all_input_leases(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            )
            result = der.exchange_reverse(out.clone())
            assert set(result.keys()) == {a.token, b.token}
            for lease in result.values():
                lease.discard()
            out.discard()

    def test_reverse_returned_leases_are_active(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            )
            result = der.exchange_reverse(out.clone())
            for tok, lease in result.items():
                lease.ensure_active()
                assert lease.subject is tok
            for lease in result.values():
                lease.discard()
            out.discard()

    def test_reverse_returned_leases_are_independent(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            )
            result = der.exchange_reverse(out.clone())
            # Discarding one does not affect the other.
            result[a.token].discard()
            assert result[b.token].is_active()
            result[b.token].discard()
            out.discard()

    def test_reverse_takes_ownership_of_output_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward({inp.token: inp.root_lease.clone()})
            handed_in = out.clone()
            result = der.exchange_reverse(handed_in)
            assert not handed_in.is_active()
            for lease in result.values():
                lease.discard()
            out.discard()

    def test_reverse_raises_on_non_derived_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            plain = inp.root_lease.clone()
            with pytest.raises(TypeError, match="does not belong"):
                der.exchange_reverse(plain)  # type: ignore[arg-type]

    def test_reverse_raises_on_foreign_output_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der1", inputs={inp.token}).revoke_after_block() as der1,
            DerivedLocalLifetime("der2", inputs={inp.token}).revoke_after_block() as der2,
        ):
            o1 = der1.exchange_forward({inp.token: inp.root_lease.clone()})
            with pytest.raises(TypeError, match="does not belong"):
                der2.exchange_reverse(o1.clone())
            o1.discard()

    def test_reverse_raises_on_discarded_output_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            o = der.exchange_forward({inp.token: inp.root_lease.clone()})
            dead = o.clone()
            dead.discard()
            with pytest.raises(LeaseDiscardedError):
                der.exchange_reverse(dead)
            o.discard()


# ---------------------------------------------------------------------------
# subclone / subtake
# ---------------------------------------------------------------------------


class TestDerivedSubclone:
    def test_subclone_returns_live_via_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            )
            cloned = der.subclone(out, a.token)
            assert cloned.subject is a.token
            assert cloned.is_active()
            cloned.discard()
            out.discard()

    def test_subclone_does_not_consume_out_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            )
            cloned_a = der.subclone(out, a.token)
            assert out.is_active()
            # A second subclone for a different input still works.
            cloned_b = der.subclone(out, b.token)
            assert out.is_active()
            # A subsequent exchange_reverse on the same lease still
            # returns the full dict.
            cloned_a.discard()
            cloned_b.discard()
            inputs = der.exchange_reverse(out)
            assert set(inputs.keys()) == {a.token, b.token}
            for v in inputs.values():
                v.discard()

    def test_subclone_raises_for_unknown_target(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("other").revoke_after_block() as other,
            DerivedLocalLifetime("der", inputs={a.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward({a.token: a.root_lease.clone()})
            with pytest.raises(KeyError, match="not an input"):
                der.subclone(out, other.token)
            assert out.is_active()
            out.discard()

    def test_subclone_raises_on_non_derived_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            plain = inp.root_lease.clone()
            with pytest.raises(TypeError, match="does not belong"):
                der.subclone(plain, inp.token)  # type: ignore[arg-type]
            plain.discard()

    def test_subclone_raises_on_foreign_output_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der1", inputs={inp.token}).revoke_after_block() as der1,
            DerivedLocalLifetime("der2", inputs={inp.token}).revoke_after_block() as der2,
        ):
            o1 = der1.exchange_forward({inp.token: inp.root_lease.clone()})
            with pytest.raises(TypeError, match="does not belong"):
                der2.subclone(o1, inp.token)
            o1.discard()

    def test_subclone_raises_on_discarded_output_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            o = der.exchange_forward({inp.token: inp.root_lease.clone()})
            dead = o.clone()
            dead.discard()
            with pytest.raises(LeaseDiscardedError):
                der.subclone(dead, inp.token)
            o.discard()


class TestDerivedSubtake:
    def test_subtake_returns_live_via_lease_and_consumes_out(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            )
            taken = der.subtake(out, a.token)
            assert taken.subject is a.token
            assert taken.is_active()
            assert not out.is_active()
            taken.discard()

    def test_subtake_discards_other_vias(self) -> None:
        # If subtake leaked the other vias, the lease-leak fixture (autouse) would
        # report a leak when we exit the block. Just doing the operation and
        # cleanly discarding the survivor proves the discard happened.
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            UnconditionalLocalLifetime("c").revoke_after_block() as c,
            DerivedLocalLifetime(
                "der", inputs={a.token, b.token, c.token}
            ).revoke_after_block() as der,
        ):
            out = der.exchange_forward(
                {
                    a.token: a.root_lease.clone(),
                    b.token: b.root_lease.clone(),
                    c.token: c.root_lease.clone(),
                }
            )
            taken = der.subtake(out, b.token)
            taken.discard()

    def test_subtake_raises_for_unknown_target(self) -> None:
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("other").revoke_after_block() as other,
            DerivedLocalLifetime("der", inputs={a.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward({a.token: a.root_lease.clone()})
            handed_in = out.clone()
            with pytest.raises(KeyError, match="not an input"):
                der.subtake(handed_in, other.token)
            # ``take()`` runs before validation, so handed_in is consumed even
            # on the error path. The original ``out`` remains usable.
            assert out.is_active()
            out.discard()

    def test_subtake_raises_on_non_derived_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            plain = inp.root_lease.clone()
            with pytest.raises(TypeError, match="does not belong"):
                der.subtake(plain, inp.token)  # type: ignore[arg-type]

    def test_subtake_raises_on_foreign_output_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der1", inputs={inp.token}).revoke_after_block() as der1,
            DerivedLocalLifetime("der2", inputs={inp.token}).revoke_after_block() as der2,
        ):
            o1 = der1.exchange_forward({inp.token: inp.root_lease.clone()})
            with pytest.raises(TypeError, match="does not belong"):
                der2.subtake(o1.clone(), inp.token)
            o1.discard()

    def test_subtake_raises_on_discarded_output_lease(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            o = der.exchange_forward({inp.token: inp.root_lease.clone()})
            dead = o.clone()
            dead.discard()
            with pytest.raises(LeaseDiscardedError):
                der.subtake(dead, inp.token)
            o.discard()


# ---------------------------------------------------------------------------
# Input-end cascade
# ---------------------------------------------------------------------------


class TestDerivedInputEndCascade:
    def test_input_end_breaks_all_output_leases(self) -> None:
        inp = UnconditionalLocalLifetime("inp")
        with DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der:
            o1 = der.exchange_forward({inp.token: inp.root_lease.clone()})
            o2 = der.exchange_forward({inp.token: inp.root_lease.clone()})
            reasons: list[str] = []
            o1.register_broken_callback(lambda r: reasons.append(r))
            o2.register_broken_callback(lambda r: reasons.append(r))
            inp.revoke("gone")
            assert len(reasons) == 2
            for r in reasons:
                assert "ended" in r
            assert der.token.ended()

    def test_input_end_closes_output_token(self) -> None:
        inp = UnconditionalLocalLifetime("inp")
        with DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der:
            ended: list[None] = []
            der.token.register_end_callback(lambda: ended.append(None))
            inp.revoke("gone")
            assert ended == [None]

    def test_input_end_leaves_other_inputs_untouched(self) -> None:
        with UnconditionalLocalLifetime("a").revoke_after_block() as a:
            b = UnconditionalLocalLifetime("b")
            with DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der:
                _ = der  # output built just for side-effect of registration
                b.revoke("bye b")
                assert der.token.ended()
                assert not a.token.ended()

    def test_input_end_releases_vias(self) -> None:
        """After input-end cascade, surviving inputs can close naturally."""
        with (
            UnconditionalLocalLifetime("a").revoke_after_block() as a,
            UnconditionalLocalLifetime("b").revoke_after_block() as b,
            DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der,
        ):
            # Forward some leases — vias now hold clones of a.root_lease and b.root_lease.
            der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            ).discard()
            der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            ).discard()
            # No live outputs now; revoke a. b's vias from any live outputs are gone
            # because we discarded them; b's root lease is still held by the CM.
            # Revoke a as input-end trigger.
            a.revoke("gone")
            # b still alive.
            assert not b.token.ended()

    def test_multi_input_first_one_to_end_wins(self) -> None:
        a = UnconditionalLocalLifetime("a")
        b = UnconditionalLocalLifetime("b")
        with DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der:
            out = der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            )
            reasons: list[str] = []
            out.register_broken_callback(lambda r: reasons.append(r))
            a.revoke("a bye")
            assert der.token.ended()
            assert len(reasons) == 1
            # Ending b now is a no-op at the Derived level.
            b.revoke("b bye")
            assert der.token.ended()


# ---------------------------------------------------------------------------
# Output-drain idle semantics
# ---------------------------------------------------------------------------


class TestDerivedOutputIdle:
    def test_all_outputs_drained_while_inputs_live_keeps_output_alive(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            o1 = der.exchange_forward({inp.token: inp.root_lease.clone()})
            o2 = der.exchange_forward({inp.token: inp.root_lease.clone()})
            o1.discard()
            o2.discard()
            # No live outputs, but input alive → Derived is idle, not closed.
            assert not der.token.ended()
            # New forward exchange still works.
            o3 = der.exchange_forward({inp.token: inp.root_lease.clone()})
            assert o3.is_active()
            o3.discard()


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


class TestDerivedRevoke:
    def test_revoke_breaks_all_outputs_and_ends_token(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            out = der.exchange_forward({inp.token: inp.root_lease.clone()})
            reasons: list[str] = []
            out.register_broken_callback(lambda r: reasons.append(r))
            der.revoke("bye")
            assert der.token.ended()
            assert reasons == ["bye"]

    def test_revoke_is_idempotent(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            der.revoke("first")
            der.revoke("second")  # no raise

    def test_revoke_after_natural_close_is_idempotent(self) -> None:
        inp = UnconditionalLocalLifetime("inp")
        with DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der:
            inp.revoke("bye")  # closes derived via input-end
            assert der.token.ended()
            der.revoke("post-close")  # idempotent

    def test_revoke_does_not_touch_inputs(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            der.revoke("bye")
            assert not inp.token.ended()


# ---------------------------------------------------------------------------
# Dead on arrival
# ---------------------------------------------------------------------------


class TestDerivedDeadOnArrival:
    def test_already_ended_input_closes_output_at_construction(self) -> None:
        with UnconditionalLocalLifetime("inp").revoke_after_block() as inp:
            inp.revoke("gone before use")
            with DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der:
                assert der.token.ended()

    def test_forward_on_doa_raises(self) -> None:
        with UnconditionalLocalLifetime("inp").revoke_after_block() as inp:
            inp.revoke("gone")
            with DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der:
                with pytest.raises(LeaseBrokenError):
                    der.exchange_forward({inp.token: inp.root_lease.clone()})

    def test_empty_inputs_is_not_doa(self) -> None:
        with DerivedLocalLifetime("der", inputs=set()).revoke_after_block() as der:
            assert not der.token.ended()


# ---------------------------------------------------------------------------
# _via lifecycle (claim on every input)
# ---------------------------------------------------------------------------


class TestDerivedViasLifecycle:
    def test_output_lease_holds_claim_on_each_input(self) -> None:
        """Every live output lease keeps all input lifetimes alive via its vias."""
        a = UnconditionalLocalLifetime("a")
        b = UnconditionalLocalLifetime("b")
        with DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der:
            out = der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            )
            # Discard both inputs' own roots: they stay alive via out's vias.
            a.root_lease.discard()
            b.root_lease.discard()
            assert not a.token.ended()
            assert not b.token.ended()
            # Discard the output: both inputs can now close.
            out.discard()
            assert a.token.ended()
            assert b.token.ended()

    def test_cloned_output_holds_independent_claim_per_input(self) -> None:
        a = UnconditionalLocalLifetime("a")
        with DerivedLocalLifetime("der", inputs={a.token}).revoke_after_block() as der:
            out = der.exchange_forward({a.token: a.root_lease.clone()})
            out_clone = out.clone()
            a.root_lease.discard()
            out.discard()
            assert not a.token.ended()  # out_clone's via still claims a
            out_clone.discard()
            assert a.token.ended()

    def test_revoke_releases_all_vias(self) -> None:
        a = UnconditionalLocalLifetime("a")
        b = UnconditionalLocalLifetime("b")
        with DerivedLocalLifetime("der", inputs={a.token, b.token}).revoke_after_block() as der:
            out = der.exchange_forward(
                {a.token: a.root_lease.clone(), b.token: b.root_lease.clone()}
            )
            a.root_lease.discard()
            b.root_lease.discard()
            assert not a.token.ended()
            assert not b.token.ended()
            der.revoke("bye")
            # Revoke released all vias → inputs can close.
            assert a.token.ended()
            assert b.token.ended()
            # out is now broken; discard is a safe no-op.
            out.discard()


# ---------------------------------------------------------------------------
# Structural checks (defensive; should never fire in correct code)
# ---------------------------------------------------------------------------


class TestDerivedStructuralChecks:
    def test_foreign_output_lease_rejected_by_derived(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der1", inputs={inp.token}).revoke_after_block() as der1,
            DerivedLocalLifetime("der2", inputs={inp.token}).revoke_after_block() as der2,
        ):
            o = der1.exchange_forward({inp.token: inp.root_lease.clone()})
            with pytest.raises(RuntimeError, match="different DerivedLocalLifetime"):
                der2._register_lease(o)  # type: ignore[arg-type]
            o.discard()

    def test_unrecognized_grant_rejected(self) -> None:
        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            # An ordinary LocalGrant (not a _DerivedOutputGrant) is unrecognized.
            with pytest.raises(RuntimeError, match="unrecognized grant"):
                der._register_lease(inp.root_lease)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


class TestDerivedAclose:
    @pytest.mark.anyio
    async def test_aclose_returns_immediately_if_already_ended(self) -> None:
        import anyio

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            inp = UnconditionalLocalLifetime("inp")
            der = DerivedLocalLifetime("der", inputs={inp.token})
            inp.revoke("gone")
            assert der.token.ended()
            await der.aclose()

    @pytest.mark.anyio
    async def test_aclose_waits_for_input_end(self) -> None:
        import anyio

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            inp = UnconditionalLocalLifetime("inp")
            der = DerivedLocalLifetime("der", inputs={inp.token})

            finished = False

            async def closer() -> None:
                nonlocal finished
                await der.aclose()
                finished = True

            async with anyio.create_task_group() as tg:
                tg.start_soon(closer)
                await anyio.sleep(0.01)
                assert not finished
                assert not der.token.ended()
                inp.revoke("gone")  # triggers input-end cascade → derived ends
            assert finished
            assert der.token.ended()

    @pytest.mark.anyio
    async def test_aclose_revokes_on_cancellation(self) -> None:
        import anyio

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            inp = UnconditionalLocalLifetime("inp")
            with DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der:
                out = der.exchange_forward({inp.token: inp.root_lease.clone()})
                async with anyio.create_task_group() as tg:
                    tg.start_soon(der.aclose)
                    await anyio.sleep(0.01)
                    tg.cancel_scope.cancel()
                assert der.token.ended()
                assert not out.is_active()
                out.discard()
            # inp still alive (der.revoke doesn't touch it).
            assert not inp.token.ended()
            inp.revoke("teardown")

    @pytest.mark.anyio
    async def test_aclose_is_idempotent(self) -> None:
        import anyio

        with anyio.fail_after(_TEST_DEADLOCK_TIMEOUT):
            inp = UnconditionalLocalLifetime("inp")
            with DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der:
                inp.revoke("gone")
                await der.aclose()
                await der.aclose()


# ---------------------------------------------------------------------------
# Leak detection: finalizer-fired warning + implicit cleanup
# ---------------------------------------------------------------------------


class TestLeakWarnings:
    """Garbage-collecting a lease without discarding it logs a warning and triggers the
    same cleanup path as an explicit discard — the token still closes if it was the last
    holder, and any via claim on an input lifetime is released."""

    @pytest.mark.allow_lease_leak
    def test_unconditional_leaked_lease_warns_and_auto_closes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import gc

        # Flush any stray unreachable-but-uncollected objects from earlier tests before we
        # open the caplog window, so our `gc.collect()` below only fires finalizers for
        # leases we intentionally leaked inside this test.
        gc.collect()

        with UnconditionalLocalLifetime("leaktest").revoke_after_block() as lt:
            # Leak a clone: create it inside a sub-scope so Python's local lookup drops it
            # when the sub-function returns.
            def leak_it() -> None:
                lt.root_lease.clone()  # return value dropped immediately

            with caplog.at_level("WARNING", logger="fxtr2.leases.local_lease"):
                leak_it()
                gc.collect()
            # Exactly one warning, for the leaked clone.
            leak_records = [r for r in caplog.records if "leaked without discard" in r.message]
            assert len(leak_records) == 1
            # The lifetime is still alive because the root lease is still held.
            assert not lt.token.ended()

    @pytest.mark.allow_lease_leak
    def test_derived_output_leak_releases_vias(self, caplog: pytest.LogCaptureFixture) -> None:
        import gc

        # Flush any stray unreachable-but-uncollected objects from earlier tests before we
        # open the caplog window, so our `gc.collect()` below only fires finalizers for
        # leases we intentionally leaked inside this test.
        gc.collect()

        with (
            UnconditionalLocalLifetime("inp").revoke_after_block() as inp,
            DerivedLocalLifetime("der", inputs={inp.token}).revoke_after_block() as der,
        ):
            # Leak an output lease: its cleanup must discard the via on inp so that
            # discarding inp's own root truly ends the input lifetime.
            def leak_output() -> None:
                der.exchange_forward({inp.token: inp.root_lease.clone()})

            with caplog.at_level("WARNING", logger="fxtr2.leases.local_lease"):
                leak_output()
                gc.collect()
            leak_records = [r for r in caplog.records if "leaked without discard" in r.message]
            assert len(leak_records) == 1
            # After GC, the output lease's via has been discarded. So discarding inp's
            # own root lease should take the input to ended.
            inp.root_lease.discard()
            assert inp.token.ended()


# ---------------------------------------------------------------------------
# STATIC_LIFETIME
# ---------------------------------------------------------------------------


class TestStaticLifetime:
    def test_token_never_ended(self) -> None:
        from fxtr2.leases.local_lease import STATIC_LIFETIME

        assert not STATIC_LIFETIME.token.ended()

    def test_lease_is_active(self) -> None:
        from fxtr2.leases.local_lease import STATIC_LIFETIME

        lease = STATIC_LIFETIME.lease()
        assert lease.is_active()
        lease.ensure_active()

    def test_lease_factory_returns_distinct_leases_sharing_grant(self) -> None:
        from fxtr2.leases.local_lease import STATIC_LIFETIME

        a = STATIC_LIFETIME.lease()
        b = STATIC_LIFETIME.lease()
        assert a is not b
        assert a.grant is b.grant

    def test_clone_is_new_object_sharing_grant(self) -> None:
        from fxtr2.leases.local_lease import STATIC_LIFETIME

        lease = STATIC_LIFETIME.lease()
        c = lease.clone()
        assert c is not lease
        assert c.grant is lease.grant

    def test_take_returns_fresh_lease_and_discards_original(self) -> None:
        from fxtr2.leases.local_lease import STATIC_LIFETIME

        lease = STATIC_LIFETIME.lease()
        t = lease.take()
        assert t is not lease
        assert not lease.is_active()
        assert t.is_active()
        assert t.grant is lease.grant

    def test_discard_idempotent(self) -> None:
        from fxtr2.leases.local_lease import STATIC_LIFETIME

        lease = STATIC_LIFETIME.lease()
        lease.discard()
        lease.discard()
        assert not lease.is_active()

    def test_grants_compare_equal_across_clones(self) -> None:
        from fxtr2.leases.local_lease import STATIC_LIFETIME

        lease_a = STATIC_LIFETIME.lease().clone()
        lease_b = STATIC_LIFETIME.lease().clone()
        assert lease_a.grant == lease_b.grant

    def test_register_broken_callback_never_fires(self) -> None:
        from fxtr2.leases.local_lease import STATIC_LIFETIME

        fired: list[str] = []
        deregister = STATIC_LIFETIME.lease().register_broken_callback(
            lambda reason: fired.append(reason)
        )
        # The callback is silently dropped; deregister is a no-op function.
        assert callable(deregister)
        deregister()
        assert fired == []

    def test_register_end_callback_never_fires(self) -> None:
        from fxtr2.leases.local_lease import STATIC_LIFETIME

        fired: list[None] = []
        deregister = STATIC_LIFETIME.token.register_end_callback(lambda: fired.append(None))
        assert callable(deregister)
        deregister()
        assert fired == []
