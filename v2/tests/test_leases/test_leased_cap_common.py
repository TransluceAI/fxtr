"""Tests for fxtr2.leases.leased_cap_common — Cap-IR helpers.

Covers `copy_ir_value`, the private `discard_leases_in` walker, and `LocalPendingResultBox`
(state machine + `await_resolution` + lifetime-end cleanup). Will grow further as
`IRFutureLocalImpl` / `IRHandleLocalImplBase` are filled in.

Tests use `revoke_after_block()` to ensure lifetimes close cleanly, so stray leases from one
test can't fire leak warnings that pollute unrelated tests' log captures.
"""

from __future__ import annotations

from typing import Any, cast

import anyio
import anyio.abc
import pytest

from fxtr2.leases.lease_types import Lease, LeaseBrokenError, LeaseDiscardedError, LifetimeToken

# `open_lifetime_task_group` is re-imported here (its tests live in `test_lease_util.py`)
# because the Cap-IR tests use it as a convenience to set up (lifetime, task_group) pairs.
from fxtr2.leases.lease_util import open_lifetime_task_group
from fxtr2.leases.leased_cap_common import (
    IRFutureLocalImpl,
    IRHandleLocalImplBase,
    LocalPendingResultBox,
    PendingResultBox,
    UnwrappedCallArg,
    _copy_request,
    copy_ir_value,
    discard_leases_in,
    spawn_ir_future_in_task_group,
)
from fxtr2.leases.leased_cap_ir import (
    Call,
    GetItem,
    IRCallArg,
    IRException,
    IRFuture,
    IRHandle,
    IRTagged,
    IRValue,
    Request,
    UnwrapForPipelining,
    WaitAndIgnoreFutureThen,
)
from fxtr2.leases.local_lease import LocalLifetimeToken, UnconditionalLocalLifetime

pytestmark = pytest.mark.anyio


async def _await_via_lease(
    box: LocalPendingResultBox[Any, Any],
    attr_path: tuple[str | int, ...],
    lease: Lease[Any],
) -> Any:
    """Test-only adapter for the pre-register_break API shape.

    The box now takes a break-registering callable rather than a lease directly;
    most tests were written against the old "pass a lease, box consumes it"
    contract. This helper takes the lease (which raises if already-broken /
    discarded, preserving the old validation behaviour at call sites) and wires
    its ``register_broken_callback`` through as the break signal.
    """
    with lease.take() as lease:
        return await box.await_resolution(attr_path, register_break=lease.register_broken_callback)


# ---------------------------------------------------------------------------
# Test stubs for IRHandle / IRFuture (both are ABCs).
# ---------------------------------------------------------------------------


class _StubHandle(IRHandle[Any]):
    def __init__(self, token: LifetimeToken[Any]) -> None:
        self._token = token

    @property
    def token(self) -> LifetimeToken[Any]:
        return self._token

    async def request(
        self,
        lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],  # noqa: ARG002
        request: Request,  # noqa: ARG002
    ) -> IRValue:
        raise NotImplementedError

    def dispatch(
        self,
        lease: Lease[Any] | UnwrapForPipelining[Lease[Any], Any],  # noqa: ARG002
        request: Request,  # noqa: ARG002
    ) -> Any:
        raise NotImplementedError


class _StubFuture(IRFuture[Any, Any]):
    def __init__(self, token: LifetimeToken[Any]) -> None:
        self._token = token

    @property
    def token(self) -> LifetimeToken[Any]:
        return self._token

    async def wait(self, lease: Lease[Any]) -> Any:  # noqa: ARG002
        raise NotImplementedError

    def walk(self, attr_path: Any) -> Any:  # noqa: ARG002
        raise NotImplementedError

    def pipeline_dispatch(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Primitives — immutable, shared by reference.
# ---------------------------------------------------------------------------


class TestCopyPrimitives:
    @pytest.mark.parametrize(
        "value",
        [None, True, False, 0, 1, -42, 3.14, float("inf"), "", "hello", b"", b"bytes"],
    )
    def test_primitives_round_trip(self, value: Any) -> None:
        out = copy_ir_value(value, take=False)
        assert out == value

    def test_bytes_shared(self) -> None:
        # Primitives are shared by reference (they're immutable anyway).
        data = b"some bytes"
        assert copy_ir_value(data, take=False) is data


# ---------------------------------------------------------------------------
# Handles / futures / tokens — shared by reference (stable identity).
# ---------------------------------------------------------------------------


class TestCopyStableReferences:
    def test_token_shared(self) -> None:
        token = LocalLifetimeToken("t")  # raw token, no lease attached
        assert copy_ir_value(token, take=False) is token

    def test_handle_shared(self) -> None:
        token = LocalLifetimeToken("t")
        handle = _StubHandle(token)
        assert copy_ir_value(handle, take=False) is handle

    def test_future_shared(self) -> None:
        token = LocalLifetimeToken("t")
        future = _StubFuture(token)
        assert copy_ir_value(future, take=False) is future


# ---------------------------------------------------------------------------
# Leases — clone or take.
# ---------------------------------------------------------------------------


class TestCopyLease:
    def test_clone_mode_keeps_original_active(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            src = lt.root_lease.clone()
            dst = copy_ir_value(src, take=False)
            assert isinstance(dst, Lease)
            assert dst is not src
            assert src.is_active()
            assert dst.is_active()
            assert dst.grant is src.grant  # clones share grant
            src.discard()
            dst.discard()

    def test_take_mode_consumes_original(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            src = lt.root_lease.clone()
            dst = copy_ir_value(src, take=True)
            assert not src.is_active()
            assert isinstance(dst, Lease)
            assert dst.is_active()
            assert dst.grant is src.grant
            dst.discard()

    def test_copy_can_be_independently_discarded(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            src = lt.root_lease.clone()
            dst = copy_ir_value(src, take=False)
            assert isinstance(dst, Lease)
            dst.discard()
            assert src.is_active()
            src.discard()


# ---------------------------------------------------------------------------
# Containers — mutable, deep-copied.
# ---------------------------------------------------------------------------


class TestCopyContainers:
    def test_empty_list(self) -> None:
        out = copy_ir_value([], take=False)
        assert out == []
        assert isinstance(out, list)

    def test_empty_dict(self) -> None:
        out = copy_ir_value({}, take=False)
        assert out == {}
        assert isinstance(out, dict)

    def test_list_is_new_list(self) -> None:
        src = [1, 2, 3]
        out = copy_ir_value(src, take=False)
        assert out == [1, 2, 3]
        assert out is not src
        assert isinstance(out, list)
        out.append(4)
        assert src == [1, 2, 3]  # original unchanged

    def test_dict_is_new_dict(self) -> None:
        src = {"a": 1, "b": 2}
        out = copy_ir_value(src, take=False)
        assert out == {"a": 1, "b": 2}
        assert out is not src
        assert isinstance(out, dict)
        out["c"] = 3
        assert "c" not in src

    def test_nested_containers_deep_copied(self) -> None:
        src = {"xs": [1, [2, 3]], "ys": {"k": [4]}}
        out = copy_ir_value(src, take=False)
        assert out == src
        assert isinstance(out, dict)
        inner_xs = out["xs"]
        assert isinstance(inner_xs, list)
        inner_inner = inner_xs[1]
        assert isinstance(inner_inner, list)
        inner_inner.append(999)
        assert 999 not in src["xs"][1]

    def test_leases_in_nested_container(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            a = lt.root_lease.clone()
            b = lt.root_lease.clone()
            src = [a, {"k": b}]
            out = copy_ir_value(src, take=False)
            assert isinstance(out, list)
            out_a, out_d = out[0], out[1]
            assert isinstance(out_a, Lease)
            assert out_a is not a
            assert out_a.grant is a.grant
            assert isinstance(out_d, dict)
            out_b = out_d["k"]
            assert isinstance(out_b, Lease)
            assert out_b.grant is b.grant
            # Originals still active.
            assert a.is_active() and b.is_active()
            for lease in (a, b, out_a, out_b):
                lease.discard()


# ---------------------------------------------------------------------------
# IRTagged — tag shared, value recursed.
# ---------------------------------------------------------------------------


class TestCopyIRTagged:
    def test_tagged_value_deep_copied(self) -> None:
        src = IRTagged(tag="my-tag", value=[1, 2, 3])
        out = copy_ir_value(src, take=False)
        assert isinstance(out, IRTagged)
        assert out.tag == "my-tag"
        assert out.value == [1, 2, 3]
        assert out.value is not src.value  # inner list is new

    def test_tagged_with_lease(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            src = IRTagged(tag=42, value=lease)
            out = copy_ir_value(src, take=False)
            assert isinstance(out, IRTagged)
            assert out.tag == 42
            out_lease = out.value
            assert isinstance(out_lease, Lease)
            assert out_lease is not lease
            assert out_lease.grant is lease.grant
            assert lease.is_active()
            lease.discard()
            out_lease.discard()


# ---------------------------------------------------------------------------
# IRException — message/category preserved, data recursed.
# ---------------------------------------------------------------------------


class TestCopyIRException:
    def test_message_and_category_preserved(self) -> None:
        exc = IRException("oops", category="my-cat", data=None)
        out = copy_ir_value(exc, take=False)
        assert isinstance(out, IRException)
        assert out.message == "oops"  # raw message round-trips
        assert out.category == "my-cat"
        assert str(out) == "[my-cat] oops"  # category-prefixed display
        assert out.data is None

    def test_data_deep_copied(self) -> None:
        # `IRException`'s `D` parameter is unconstrained, but `IRCallArg` only allows
        # `IRException[IRStatic]`. We cast to `IRCallArg` at the test boundary to cover the
        # "data is a mutable IR tree" case the helper is designed to handle.
        exc = IRException("oops", category=None, data={"x": [1, 2, 3]})
        out = copy_ir_value(cast(IRCallArg, exc), take=False)
        assert isinstance(out, IRException)
        out_data = cast(dict[str, list[int]], out.data)
        src_data = cast(dict[str, list[int]], exc.data)
        assert out_data == {"x": [1, 2, 3]}
        assert out_data is not src_data
        out_data["x"].append(4)
        assert 4 not in src_data["x"]

    def test_data_with_lease_cloned(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            exc = IRException("bad", category=None, data=lease)
            out = copy_ir_value(cast(IRCallArg, exc), take=False)
            assert isinstance(out, IRException)
            data = out.data
            assert isinstance(data, Lease)
            assert data is not lease
            assert data.grant is lease.grant
            assert lease.is_active()
            lease.discard()
            data.discard()


# ---------------------------------------------------------------------------
# UnwrapForPipelining — future shared, lease cloned/taken.
# ---------------------------------------------------------------------------


class TestCopyUnwrapForPipelining:
    def test_future_shared_lease_cloned(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            token = LocalLifetimeToken("fut")
            future = _StubFuture(token)
            lease = lt.root_lease.clone()
            src = UnwrapForPipelining(future, lease)

            out = copy_ir_value(src, take=False)

            assert isinstance(out, UnwrapForPipelining)
            assert out.future is future  # shared
            assert out.lease is not lease  # new copy
            assert out.lease.grant is lease.grant
            assert lease.is_active()
            lease.discard()
            out.lease.discard()

    def test_future_shared_lease_taken(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            token = LocalLifetimeToken("fut")
            future = _StubFuture(token)
            lease = lt.root_lease.clone()
            src = UnwrapForPipelining(future, lease)

            out = copy_ir_value(src, take=True)

            assert isinstance(out, UnwrapForPipelining)
            assert out.future is future
            assert not lease.is_active()
            assert out.lease.is_active()
            out.lease.discard()


# ---------------------------------------------------------------------------
# Unsupported types.
# ---------------------------------------------------------------------------


class TestCopyUnsupported:
    def test_bare_object_raises(self) -> None:
        with pytest.raises(TypeError, match="unsupported value"):
            copy_ir_value(object(), take=False)  # type: ignore[arg-type]

    def test_set_raises(self) -> None:
        # Sets aren't in IRCallArg; should raise even though they're iterable.
        with pytest.raises(TypeError, match="unsupported value"):
            copy_ir_value({1, 2, 3}, take=False)  # type: ignore[arg-type]

    def test_partial_failure_on_unsupported_nested_type(self) -> None:
        """Unsupported nested types bubble up. Note that any leases already copied before the
        failure are leaked (the weakref finalizer will clean them up with a warning)."""
        with pytest.raises(TypeError, match="unsupported value"):
            copy_ir_value([1, object()], take=False)  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Integration: request-style nested tree round-trip.
# ---------------------------------------------------------------------------


class TestCopyIntegration:
    def test_realistic_call_arg(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease_a = lt.root_lease.clone()
            lease_b = lt.root_lease.clone()
            arg = {
                "op": "batch",
                "items": [
                    {"lease": lease_a, "count": 3},
                    {"lease": lease_b, "count": 7},
                ],
                "meta": IRTagged(tag="v1", value={"retries": 2}),
            }
            out = copy_ir_value(arg, take=False)
            assert isinstance(out, dict)
            assert out["op"] == "batch"
            out_items = out["items"]
            assert isinstance(out_items, list)
            assert len(out_items) == 2
            for orig_item, new_item in zip(arg["items"], out_items, strict=True):
                assert isinstance(orig_item, dict)
                assert isinstance(new_item, dict)
                orig_lease = orig_item["lease"]
                new_lease = new_item["lease"]
                assert isinstance(orig_lease, Lease)
                assert isinstance(new_lease, Lease)
                assert new_lease is not orig_lease
                assert new_lease.grant is orig_lease.grant
                assert new_item["count"] == orig_item["count"]
            out_meta = out["meta"]
            assert isinstance(out_meta, IRTagged)
            assert out_meta.tag == "v1"
            # Discarding the copies leaves the originals active; then we discard those too.
            for new_item in out_items:
                assert isinstance(new_item, dict)
                new_lease = new_item["lease"]
                assert isinstance(new_lease, Lease)
                new_lease.discard()
            assert lease_a.is_active() and lease_b.is_active()
            lease_a.discard()
            lease_b.discard()


# ---------------------------------------------------------------------------
# discard_leases_in
# ---------------------------------------------------------------------------


class TestDiscardLeasesIn:
    def test_primitives_noop(self) -> None:
        # Should not raise on primitives.
        for v in (None, True, 42, 3.14, "s", b"b"):
            discard_leases_in(v)

    def test_discards_bare_lease(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            discard_leases_in(lease)
            assert not lease.is_active()

    def test_discards_leases_in_list(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            a = lt.root_lease.clone()
            b = lt.root_lease.clone()
            discard_leases_in([a, 42, b, "x"])
            assert not a.is_active()
            assert not b.is_active()

    def test_discards_leases_in_dict(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            a = lt.root_lease.clone()
            discard_leases_in({"k": a, "n": 1})
            assert not a.is_active()

    def test_discards_leases_in_tagged(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            a = lt.root_lease.clone()
            discard_leases_in(IRTagged(tag="t", value=a))
            assert not a.is_active()

    def test_discards_leases_in_exception_data(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            a = lt.root_lease.clone()
            exc: IRException[Any] = IRException("oops", category=None, data={"l": a})
            discard_leases_in(exc)
            assert not a.is_active()

    def test_discards_leases_in_unwrap_for_pipelining(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            token = LocalLifetimeToken("fut")
            future = _StubFuture(token)
            a = lt.root_lease.clone()
            discard_leases_in(UnwrapForPipelining(future, a))
            assert not a.is_active()

    def test_shared_refs_skipped(self) -> None:
        # Handles/futures/tokens don't hold leases themselves — they should be traversed
        # (or skipped) without raising.
        token = LocalLifetimeToken("t")
        discard_leases_in(token)
        discard_leases_in(_StubHandle(token))
        discard_leases_in(_StubFuture(token))

    def test_nested_deep(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            a = lt.root_lease.clone()
            b = lt.root_lease.clone()
            val = {"outer": [{"inner": [a, {"k": b}]}]}
            discard_leases_in(val)
            assert not a.is_active()
            assert not b.is_active()

    def test_unsupported_type_warns_and_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="fxtr2.leases.leased_cap_common"):
            discard_leases_in(object())  # type: ignore[arg-type]
        # Should have logged something, not raised.
        assert any("unsupported" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# LocalPendingResultBox — construction and state machine
# ---------------------------------------------------------------------------


class TestBoxConstruction:
    def test_pending_on_construction(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            assert not box.is_resolved()
            assert box.token is lt.token


class TestBoxSetResult:
    def test_set_result_stores_value(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_result({"x": 1})
            assert box.is_resolved()

    def test_set_result_takes_ownership_of_lease(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            caller_lease = lt.root_lease.clone()
            box.set_result({"l": caller_lease})
            assert not caller_lease.is_active()

    def test_set_result_twice_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_result(1)
            with pytest.raises(RuntimeError, match="already resolved"):
                box.set_result(2)

    def test_set_result_after_exception_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_exception(IRException("bad"))
            with pytest.raises(RuntimeError, match="already rejected"):
                box.set_result(1)

    def test_ir_exception_as_return_value(self) -> None:
        """Returning an IRException is a valid resolved value (not a rejection)."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            exc = IRException("returned-not-raised")
            box.set_result(exc)
            assert box.is_resolved()
            assert box.state == "resolved"


class TestBoxSetException:
    def test_set_exception_stores_exception(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_exception(IRException("oops"))
            assert box.is_resolved()
            assert box.state == "rejected"

    def test_set_exception_takes_ownership_of_leases_in_data(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            caller_lease = lt.root_lease.clone()
            exc: IRException[Any] = IRException("bad", data={"l": caller_lease})
            box.set_exception(exc)
            assert not caller_lease.is_active()

    def test_set_exception_twice_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_exception(IRException("first"))
            with pytest.raises(RuntimeError, match="already rejected"):
                box.set_exception(IRException("second"))

    def test_set_exception_after_result_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_result(1)
            with pytest.raises(RuntimeError, match="already resolved"):
                box.set_exception(IRException("late"))


# ---------------------------------------------------------------------------
# Token-end cleanup and "discarded" state
# ---------------------------------------------------------------------------


class TestBoxDiscardedState:
    def test_token_end_on_resolved_discards_leases(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        box = LocalPendingResultBox[Any, Any](lt.token)
        box.set_result({"l": lt.root_lease.clone()})
        assert box.state == "resolved"
        assert box._storage.value is not None
        lt.revoke("bye")
        assert box.state == "discarded"
        assert box._storage.value is None

    def test_token_end_on_rejected_discards_leases(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        box = LocalPendingResultBox[Any, Any](lt.token)
        lease_in_data = lt.root_lease.clone()
        box.set_exception(IRException("oops", data={"l": lease_in_data}))
        assert box.state == "rejected"
        lt.revoke("bye")
        assert box.state == "discarded"
        assert box._storage.exception is None

    def test_token_end_on_pending_just_transitions(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        box = LocalPendingResultBox[Any, Any](lt.token)
        assert box.state == "pending"
        lt.revoke("bye")
        assert box.state == "discarded"

    def test_set_result_after_discarded_drops_leases(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        lease_to_test_discard_of = lt.root_lease.clone()
        box = LocalPendingResultBox[Any, Any](lt.token)
        # Clone one more, then revoke.
        lt.revoke("bye")
        assert box.state == "discarded"
        assert not lease_to_test_discard_of.is_active()  # already broken via revoke
        # Passing a (broken) lease in should be accepted and cleaned up silently.
        box.set_result({"l": lease_to_test_discard_of})
        assert box.state == "discarded"  # unchanged

    def test_set_exception_after_discarded_drops_leases(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        box = LocalPendingResultBox[Any, Any](lt.token)
        lt.revoke("bye")
        assert box.state == "discarded"
        # Exception carrying a broken lease — should be accepted gracefully.
        exc: IRException[Any] = IRException("bad", data={"x": 1})
        box.set_exception(exc)
        assert box.state == "discarded"

    def test_resolved_event_is_set_on_discard(self) -> None:
        """So waiters wake up even if their lease-break callback doesn't fire first."""
        lt = UnconditionalLocalLifetime("lt")
        box = LocalPendingResultBox[Any, Any](lt.token)
        assert not box._resolved_event.is_set()
        lt.revoke("bye")
        assert box._resolved_event.is_set()


# ---------------------------------------------------------------------------
# PendingResultBox — explicit-discard semantics (RPC lowering uses these
# directly, without a token subscription)
# ---------------------------------------------------------------------------


class TestPendingResultBoxExplicitDiscard:
    """Direct coverage of the token-less :class:`PendingResultBox` base class.

    The RPC lowering layer's :class:`FutureImportEntry` constructs these directly and
    drives ``discard()`` itself once delivery and waiter-drain are both complete (see
    ``plans/extras/extra5_future_box_race_fix.md``). The tests below pin down the
    behaviours that matter for that layer: explicit discard releases stored leases,
    and setters become idempotent no-ops on a discarded box.
    """

    def test_construct_starts_pending(self) -> None:
        box = PendingResultBox[Any]()
        assert box.state == "pending"
        assert not box.is_resolved()
        assert not box._resolved_event.is_set()

    def test_explicit_discard_on_pending_just_transitions(self) -> None:
        box = PendingResultBox[Any]()
        box.discard()
        assert box.state == "discarded"
        assert box._resolved_event.is_set()

    def test_explicit_discard_on_resolved_releases_leases(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = PendingResultBox[Any]()
            box.set_result({"l": lt.root_lease.clone()})
            assert box.state == "resolved"
            assert box._storage.value is not None
            box.discard()
            assert box.state == "discarded"
            assert box._storage.value is None

    def test_explicit_discard_on_rejected_releases_leases(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = PendingResultBox[Any]()
            box.set_exception(IRException("oops", data={"l": lt.root_lease.clone()}))
            assert box.state == "rejected"
            assert box._storage.exception is not None
            box.discard()
            assert box.state == "discarded"
            assert box._storage.exception is None

    def test_explicit_discard_is_idempotent(self) -> None:
        box = PendingResultBox[Any]()
        box.discard()
        box.discard()
        box.discard()
        assert box.state == "discarded"

    def test_set_result_after_discard_is_noop_and_releases_leases(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = PendingResultBox[Any]()
            box.discard()
            assert box.state == "discarded"
            # Caller transferred ownership; the box should release the leases.
            box.set_result({"l": lt.root_lease.clone()})
            assert box.state == "discarded"
            # Storage stays empty; we discarded, didn't store.
            assert box._storage.value is None

    def test_set_exception_after_discard_is_noop(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = PendingResultBox[Any]()
            box.discard()
            box.set_exception(IRException("late", data={"l": lt.root_lease.clone()}))
            assert box.state == "discarded"
            assert box._storage.exception is None

    def test_resolve_from_after_discard_is_noop(self) -> None:
        with UnconditionalLocalLifetime("src").revoke_after_block() as src_lt:
            inner_box = PendingResultBox[Any]()
            inner_future = IRFutureLocalImpl[Any, Any](
                box=cast(LocalPendingResultBox[Any, Any], inner_box),
                attr_path=(),
                task_group=cast(anyio.abc.TaskGroup, None),
                expected_grant=src_lt.root_lease.grant,
            )
            box = PendingResultBox[Any]()
            box.discard()
            box.resolve_from(inner_future, src_lt.root_lease.clone())
            assert box.state == "discarded"
            # The forwarded lease was discarded by the no-op path, not stored.
            assert box._storage.forward_lease is None

    async def test_await_resolution_on_discarded_raises(self) -> None:
        box = PendingResultBox[Any]()
        box.discard()

        # Explicit discard is the same end-state as a token-end-driven
        # discard — `await_resolution` raises LeaseBrokenError. No need to
        # supply a meaningful break-registrar; the box is already resolved
        # (event set) so `_wait_until_resolved` is skipped.
        def fake_register(_cb: Any) -> Any:
            return lambda: None

        with pytest.raises(LeaseBrokenError):
            await box.await_resolution((), register_break=fake_register)


# ---------------------------------------------------------------------------
# await_resolution
# ---------------------------------------------------------------------------


class TestAwaitResolutionResolved:
    async def test_already_resolved_returns_immediately(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_result({"x": 42})
            out = await _await_via_lease(box, (), lt.root_lease.clone())
            assert out == {"x": 42}

    async def test_descend_dict_key(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_result({"x": {"y": 7}})
            assert await _await_via_lease(box, ("x",), lt.root_lease.clone()) == {"y": 7}
            assert await _await_via_lease(box, ("x", "y"), lt.root_lease.clone()) == 7

    async def test_descend_list_index(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_result([10, 20, 30])
            assert await _await_via_lease(box, (0,), lt.root_lease.clone()) == 10
            assert await _await_via_lease(box, (2,), lt.root_lease.clone()) == 30

    async def test_list_with_non_int_index_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_result([1, 2, 3])
            with pytest.raises(TypeError, match="non-integer"):
                await _await_via_lease(box, ("oops",), lt.root_lease.clone())

    async def test_descend_into_primitive_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_result(42)
            with pytest.raises(TypeError, match="cannot walk into"):
                await _await_via_lease(box, ("x",), lt.root_lease.clone())

    async def test_result_is_deep_copy(self) -> None:
        """Mutating the returned value does not affect the box's stored value."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_result({"l": [1, 2]})
            out = await _await_via_lease(box, (), lt.root_lease.clone())
            assert isinstance(out, dict)
            inner = out["l"]
            assert isinstance(inner, list)
            inner.append(999)
            again = await _await_via_lease(box, (), lt.root_lease.clone())
            assert isinstance(again, dict)
            again_inner = again["l"]
            assert isinstance(again_inner, list)
            assert 999 not in again_inner

    async def test_leases_in_result_are_fresh_clones(self) -> None:
        """Each wait returns leases that can be independently discarded."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            # The box's expected grant is the lt root grant. To put a lease INSIDE the
            # result that's distinct from the box's own lifetime, use a separate lifetime.
            with UnconditionalLocalLifetime("inner").revoke_after_block() as inner:
                box = LocalPendingResultBox[Any, Any](lt.token)
                box.set_result({"l": inner.root_lease.clone()})
                out1 = await _await_via_lease(box, (), lt.root_lease.clone())
                out2 = await _await_via_lease(box, (), lt.root_lease.clone())
                assert isinstance(out1, dict) and isinstance(out2, dict)
                l1 = out1["l"]
                l2 = out2["l"]
                assert isinstance(l1, Lease) and isinstance(l2, Lease)
                assert l1 is not l2
                assert l1.grant == l2.grant
                # Discarding one doesn't affect the other.
                l1.discard()
                assert l2.is_active()
                l2.discard()


class TestAwaitResolutionRejected:
    async def test_rejected_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_exception(IRException("nope", category="cat", data={"x": 1}))
            with pytest.raises(IRException) as excinfo:
                await _await_via_lease(box, (), lt.root_lease.clone())
            assert excinfo.value.message == "nope"
            assert excinfo.value.category == "cat"
            assert str(excinfo.value) == "[cat] nope"
            assert excinfo.value.data == {"x": 1}

    async def test_rejected_raises_fresh_each_time(self) -> None:
        """Python mutates __traceback__ on exceptions — each raise must be a fresh copy."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_exception(IRException("once"))
            e1: IRException[Any] | None = None
            e2: IRException[Any] | None = None
            try:
                await _await_via_lease(box, (), lt.root_lease.clone())
            except IRException as e:
                e1 = e
            try:
                await _await_via_lease(box, (), lt.root_lease.clone())
            except IRException as e:
                e2 = e
            assert e1 is not None and e2 is not None
            assert e1 is not e2

    async def test_rejected_raises_regardless_of_attr_path(self) -> None:
        """Exception propagates even if caller walks into a deep path."""
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_exception(IRException("top-level failure"))
            with pytest.raises(IRException, match="top-level failure"):
                await _await_via_lease(box, ("a", "b", 0), lt.root_lease.clone())


class TestAwaitResolutionWaiting:
    async def test_wait_for_delayed_set_result(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)

            async def resolver() -> None:
                await anyio.sleep(0.01)
                box.set_result("delayed")

            async with anyio.create_task_group() as tg:
                tg.start_soon(resolver)
                out = await _await_via_lease(box, (), lt.root_lease.clone())
                assert out == "delayed"

    async def test_wait_for_delayed_set_exception(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)

            async def rejecter() -> None:
                await anyio.sleep(0.01)
                box.set_exception(IRException("async-fail"))

            async with anyio.create_task_group() as tg:
                tg.start_soon(rejecter)
                with pytest.raises(IRException, match="async-fail"):
                    await _await_via_lease(box, (), lt.root_lease.clone())

    async def test_multiple_concurrent_waiters_all_wake(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            results: list[Any] = []

            async def waiter() -> None:
                out = await _await_via_lease(box, (), lt.root_lease.clone())
                results.append(out)

            async def resolver() -> None:
                await anyio.sleep(0.01)
                box.set_result("broadcast")

            async with anyio.create_task_group() as tg:
                tg.start_soon(waiter)
                tg.start_soon(waiter)
                tg.start_soon(waiter)
                tg.start_soon(resolver)

            assert results == ["broadcast"] * 3


class TestAwaitResolutionLeaseValidation:
    async def test_discarded_input_lease_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            box.set_result(1)
            dead = lt.root_lease.clone()
            dead.discard()
            with pytest.raises(LeaseDiscardedError):
                await _await_via_lease(box, (), dead)

    async def test_broken_input_lease_raises(self) -> None:
        lt = UnconditionalLocalLifetime("lt")
        box = LocalPendingResultBox[Any, Any](lt.token)
        box.set_result(1)
        lease = lt.root_lease.clone()
        lt.revoke("bye")  # breaks `lease`
        with pytest.raises(LeaseBrokenError):
            await _await_via_lease(box, (), lease)


class TestIRFutureLocalImplGrantValidation:
    """Grant validation moved out of the box and up onto `IRFutureLocalImpl.wait`."""

    async def test_wait_rejects_mismatched_grant(self) -> None:
        with (
            UnconditionalLocalLifetime("fut-lt").revoke_after_block() as fut_lt,
            UnconditionalLocalLifetime("other-lt").revoke_after_block() as other_lt,
        ):
            async with anyio.create_task_group() as tg:
                box = LocalPendingResultBox[Any, Any](fut_lt.token)
                box.set_result(1)
                future = IRFutureLocalImpl[Any, Any](
                    box,
                    attr_path=(),
                    task_group=tg,
                    expected_grant=fut_lt.root_lease.grant,
                )
                # The caller's lease is taken up-front, then the grant check raises.
                # The taken copy is discarded on the raise, so the original `bad_lease`
                # reference is marked discarded too (per `lease.take()` semantics).
                bad_lease = other_lt.root_lease.clone()
                with pytest.raises(RuntimeError, match="lease grant does not match"):
                    await future.wait(bad_lease)
                assert not bad_lease.is_active()

    async def test_pipeline_dispatch_rejects_mismatched_grant(self) -> None:
        # All input leases are taken up-front and discarded on the grant-mismatch
        # raise, so after the call the caller's references are inactive.
        with (
            UnconditionalLocalLifetime("fut-lt").revoke_after_block() as fut_lt,
            UnconditionalLocalLifetime("other-lt").revoke_after_block() as other_lt,
            UnconditionalLocalLifetime("inner-lt").revoke_after_block() as inner_lt,
        ):
            async with anyio.create_task_group() as tg:
                box = LocalPendingResultBox[Any, Any](fut_lt.token)
                future = IRFutureLocalImpl[Any, Any](
                    box,
                    attr_path=(),
                    task_group=tg,
                    expected_grant=fut_lt.root_lease.grant,
                )
                wrong_future_lease = other_lt.root_lease.clone()
                inner_lease = inner_lt.root_lease.clone()
                with pytest.raises(RuntimeError, match="lease grant does not match"):
                    future.pipeline_dispatch(
                        wrong_future_lease,
                        inner_lease,
                        GetItem("anything"),
                    )
                # Both inputs consumed by the take + discard-on-raise cleanup.
                assert not wrong_future_lease.is_active()
                assert not inner_lease.is_active()


class TestAwaitResolutionCancellation:
    async def test_lease_break_during_wait_raises(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            box = LocalPendingResultBox[Any, Any](lt.token)
            waiter_lease = lt.root_lease.clone()

            async def waiter() -> None:
                with pytest.raises(LeaseBrokenError, match="broken while waiting"):
                    await _await_via_lease(box, (), waiter_lease)

            async def revoker() -> None:
                await anyio.sleep(0.01)
                lt.revoke("bye")

            async with anyio.create_task_group() as tg:
                tg.start_soon(waiter)
                tg.start_soon(revoker)

    async def test_discarded_state_on_entry_raises(self) -> None:
        with UnconditionalLocalLifetime("outer").revoke_after_block() as outer:
            # Create a separate lifetime just for the box so we can end it before
            # awaiting.
            inner = UnconditionalLocalLifetime("box-lt")
            box = LocalPendingResultBox[Any, Any](inner.token)
            held_lease_on_inner = inner.root_lease.clone()
            inner.revoke("pre-await")
            # box is "discarded"; the old held lease on inner is now broken.
            # Pass that (broken) lease to await_resolution — fails at ensure_active.
            with pytest.raises(LeaseBrokenError):
                await _await_via_lease(box, (), held_lease_on_inner)
            _ = outer  # touch for the lint

    async def test_discarded_during_wait_raises(self) -> None:
        """Token ends while a wait is pending → the lease we're holding breaks → raise."""
        lt = UnconditionalLocalLifetime("lt")
        box = LocalPendingResultBox[Any, Any](lt.token)
        waiter_lease = lt.root_lease.clone()

        async def waiter() -> None:
            with pytest.raises(LeaseBrokenError):
                await _await_via_lease(box, (), waiter_lease)

        async def killer() -> None:
            await anyio.sleep(0.01)
            lt.revoke("mid-wait")

        async with anyio.create_task_group() as tg:
            tg.start_soon(waiter)
            tg.start_soon(killer)


# ---------------------------------------------------------------------------
# _copy_request
# ---------------------------------------------------------------------------


class TestCopyRequest:
    def test_call_with_primitive_arg(self) -> None:
        req = Call(method="foo", arg={"x": 1})
        out = _copy_request(req, take=False)
        assert isinstance(out, Call)
        assert out.method == "foo"
        assert out.arg == {"x": 1}
        assert out.arg is not req.arg  # new list/dict

    def test_call_arg_leases_cloned(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            req = Call(method="m", arg={"l": lease})
            out = _copy_request(req, take=False)
            assert isinstance(out, Call)
            out_arg = cast(dict[str, Any], out.arg)
            assert isinstance(out_arg["l"], Lease)
            assert out_arg["l"] is not lease
            assert out_arg["l"].grant is lease.grant
            assert lease.is_active()
            lease.discard()
            out_arg["l"].discard()

    def test_call_arg_leases_taken(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            req = Call(method="m", arg=lease)
            out = _copy_request(req, take=True)
            assert isinstance(out, Call)
            assert not lease.is_active()  # taken
            out_lease = cast(Lease[Any], out.arg)
            assert out_lease.is_active()
            out_lease.discard()

    def test_getitem_passthrough(self) -> None:
        req = GetItem(attr="x")
        out = _copy_request(req, take=False)
        # GetItem has no leases; we reuse the same frozen instance.
        assert out is req

    def test_wait_and_ignore_future_then_recursive(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            token = LocalLifetimeToken("fut")
            future = _StubFuture(token)
            lease = lt.root_lease.clone()
            inner_lease = lt.root_lease.clone()
            inner_call = Call(method="inner", arg={"l": inner_lease})
            wait_for = UnwrapForPipelining(future, lease)
            req = WaitAndIgnoreFutureThen(wait_for=wait_for, then=inner_call)
            out = _copy_request(req, take=False)
            assert isinstance(out, WaitAndIgnoreFutureThen)
            # wait_for's future is shared, lease is cloned
            out_wait_for = cast(UnwrapForPipelining[Any, Any], out.wait_for)
            assert out_wait_for.future is future
            assert out_wait_for.lease is not lease
            assert out_wait_for.lease.grant is lease.grant
            # then: Call with cloned lease inside
            out_then = cast(Call[Any], out.then)
            assert isinstance(out_then, Call)
            out_then_arg = cast(dict[str, Any], out_then.arg)
            assert out_then_arg["l"] is not inner_lease
            # Originals still active
            assert lease.is_active()
            assert inner_lease.is_active()
            # Cleanup
            for tmp in (lease, inner_lease, out_wait_for.lease, out_then_arg["l"]):
                assert isinstance(tmp, Lease)
                tmp.discard()


# ---------------------------------------------------------------------------
# discard_leases_in — extended Request handling
# ---------------------------------------------------------------------------


class TestDiscardLeasesInRequest:
    def test_call_arg_leases_discarded(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            lease = lt.root_lease.clone()
            req = Call(method="m", arg={"l": lease})
            discard_leases_in(req)
            assert not lease.is_active()

    def test_getitem_is_noop(self) -> None:
        discard_leases_in(GetItem(attr="x"))  # should not raise

    def test_wait_and_ignore_recursive_discard(self) -> None:
        with UnconditionalLocalLifetime("lt").revoke_after_block() as lt:
            token = LocalLifetimeToken("fut")
            future = _StubFuture(token)
            wait_lease = lt.root_lease.clone()
            inner_lease = lt.root_lease.clone()
            req = WaitAndIgnoreFutureThen(
                wait_for=UnwrapForPipelining(future, wait_lease),
                then=Call(method="inner", arg=inner_lease),
            )
            discard_leases_in(req)
            assert not wait_lease.is_active()
            assert not inner_lease.is_active()


# ---------------------------------------------------------------------------
# IRFutureLocalImpl — token / wait / walk
# ---------------------------------------------------------------------------


def _make_box_and_future[
    T: IRValue
](
    tg: anyio.abc.TaskGroup,
    value: T,
) -> tuple[
    LocalPendingResultBox[T, Any], IRFutureLocalImpl[T, Any], Lease[Any]
]:
    """Helper: create a resolved box and a matching future + root lease."""
    lt = UnconditionalLocalLifetime[Any]("fut-lt")
    box = LocalPendingResultBox[T, Any](lt.token)
    box.set_result(value)
    future = IRFutureLocalImpl[T, Any](
        box, attr_path=(), task_group=tg, expected_grant=lt.root_lease.grant
    )
    return box, future, lt.root_lease.take()


class TestIRFutureLocalImpl:
    async def test_token_is_box_token(self) -> None:
        async with anyio.create_task_group() as tg:
            _box, future, lease = _make_box_and_future(tg, 42)
            assert future.token is _box.token
            lease.discard()

    async def test_wait_returns_value(self) -> None:
        async with anyio.create_task_group() as tg:
            _box, future, lease = _make_box_and_future(tg, {"x": 7})
            out = await future.wait(lease.clone())
            assert out == {"x": 7}
            lease.discard()

    async def test_wait_takes_lease(self) -> None:
        async with anyio.create_task_group() as tg:
            _box, future, lease = _make_box_and_future(tg, "ok")
            handed_in = lease.clone()
            await future.wait(handed_in)
            assert not handed_in.is_active()
            lease.discard()

    async def test_walk_single_str_step(self) -> None:
        async with anyio.create_task_group() as tg:
            _box, future, lease = _make_box_and_future(tg, {"x": 10})
            walked = future.walk("x")
            assert await walked.wait(lease.clone()) == 10
            lease.discard()

    async def test_walk_single_int_step(self) -> None:
        async with anyio.create_task_group() as tg:
            _box, future, lease = _make_box_and_future(tg, [10, 20, 30])
            walked = future.walk(2)
            assert await walked.wait(lease.clone()) == 30
            lease.discard()

    async def test_walk_sequence(self) -> None:
        async with anyio.create_task_group() as tg:
            _box, future, lease = _make_box_and_future(tg, {"a": {"b": 99}})
            walked = future.walk(("a", "b"))
            assert await walked.wait(lease.clone()) == 99
            lease.discard()

    async def test_walk_chain(self) -> None:
        async with anyio.create_task_group() as tg:
            _box, future, lease = _make_box_and_future(tg, {"a": [{"k": "v"}]})
            walked = future.walk("a").walk(0).walk("k")
            assert await walked.wait(lease.clone()) == "v"
            lease.discard()

    async def test_walk_str_not_sequence_of_chars(self) -> None:
        """A bare str is always treated as a single step, not a sequence of chars."""
        async with anyio.create_task_group() as tg:
            _box, future, lease = _make_box_and_future(tg, {"ab": 1, "a": 999})
            walked = future.walk("ab")
            assert await walked.wait(lease.clone()) == 1
            lease.discard()

    async def test_walk_shares_box_and_task_group(self) -> None:
        async with anyio.create_task_group() as tg:
            box, future, lease = _make_box_and_future(tg, {"x": 1})
            walked = cast(IRFutureLocalImpl[Any, Any], future.walk("x"))
            assert walked._box is box
            assert walked._task_group is tg
            lease.discard()


# ---------------------------------------------------------------------------
# spawn_ir_future_in_task_group
# ---------------------------------------------------------------------------


class _CustomBaseExc(BaseException):
    """Used to test the BaseException → revoke branch without pytest-internal fallout."""


class TestSpawnIRFuture:
    async def test_success_path(self) -> None:
        async with anyio.create_task_group() as tg:

            async def work() -> IRValue:
                await anyio.sleep(0.01)
                return {"x": 42}

            future, lease = spawn_ir_future_in_task_group(name="succ", work=work, task_group=tg)
            assert await future.wait(lease.clone()) == {"x": 42}
            lease.discard()

    async def test_ir_exception_passthrough(self) -> None:
        async with anyio.create_task_group() as tg:

            async def work() -> IRValue:
                raise IRException("custom", category="MyCat", data={"a": 1})

            future, lease = spawn_ir_future_in_task_group(name="irexc", work=work, task_group=tg)
            with pytest.raises(IRException) as excinfo:
                await future.wait(lease.clone())
            assert excinfo.value.message == "custom"
            assert excinfo.value.category == "MyCat"
            assert excinfo.value.data == {"a": 1}
            lease.discard()

    async def test_plain_exception_wraps(self) -> None:
        async with anyio.create_task_group() as tg:

            async def work() -> IRValue:
                raise ValueError("oh no")

            future, lease = spawn_ir_future_in_task_group(name="plain", work=work, task_group=tg)
            with pytest.raises(IRException) as excinfo:
                await future.wait(lease.clone())
            assert excinfo.value.message == "oh no"
            assert excinfo.value.category == "ValueError"
            lease.discard()

    async def test_base_exception_revokes(self) -> None:
        """A non-Exception BaseException should revoke the lifetime + propagate."""
        captured_token: Any = None
        with pytest.raises(BaseExceptionGroup) as excinfo:
            async with anyio.create_task_group() as tg:

                async def work() -> IRValue:
                    raise _CustomBaseExc("fatal")

                future, lease = spawn_ir_future_in_task_group(name="base", work=work, task_group=tg)
                captured_token = future.token
                # Give the runner a chance to execute.
                await anyio.sleep(0.05)
                lease.discard()
        assert any(isinstance(e, _CustomBaseExc) for e in excinfo.value.exceptions)
        assert captured_token is not None and captured_token.ended()

    async def test_fire_and_forget_discard_before_completion(self) -> None:
        """Early lease discard doesn't crash the runner; result is dropped gracefully."""
        result_was_set = anyio.Event()

        async def work() -> IRValue:
            await anyio.sleep(0.02)
            result_was_set.set()
            return "late"

        async with anyio.create_task_group() as tg:
            future, lease = spawn_ir_future_in_task_group(name="fire", work=work, task_group=tg)
            captured_token = future.token
            lease.discard()  # immediate — lifetime closes, box discarded
            assert captured_token.ended()
            # Wait for the runner to finish its work (it'll try set_result on a discarded
            # box, which is a graceful no-op).
            await result_was_set.wait()


# ---------------------------------------------------------------------------
# pipeline_dispatch
# ---------------------------------------------------------------------------


def _as_handle_future(
    f: IRFuture[Any, Any],
) -> IRFutureLocalImpl[IRHandle[Any], Any]:
    """Cast helper: `spawn_ir_future_in_task_group` and `pipeline_dispatch` return
    `IRFuture[...]` typed at `IRValue` / `Any`, but `pipeline_dispatch` itself requires
    `IRFutureLocalImpl[IRHandle[Any], ...]`. At the call site a user would typically
    know from context that the future resolves to a handle; we encode that as a
    narrowing cast for test readability."""
    return cast(IRFutureLocalImpl[IRHandle[Any], Any], f)


class _EchoHandle(IRHandleLocalImplBase[Any]):
    """Test handle that echoes back details about the request. Built on
    `IRHandleLocalImplBase` — `request` / `dispatch` / `token` are inherited; only
    `_run_request_async` is supplied.

    The subclass follows the transfer convention: it takes ownership of the lease and
    any leases inside the arg.
    """

    def __init__(self, token: LocalLifetimeToken[Any], task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[UnwrappedCallArg] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call):
            # Pass `arg` through verbatim — the base class's `set_result` takes
            # ownership of any leases inside via its return-path copy, so we don't
            # need to copy here.
            return cast(
                IRValue,
                {"echoed": "call", "method": request.method, "arg": request.arg},
            )
        return cast(IRValue, {"echoed": "getitem", "attr": request.attr})


class TestPipelineDispatch:
    async def test_basic_call(self) -> None:
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _EchoHandle(hlt.token, tg)

                async def produce_handle() -> IRValue:
                    return handle

                future, fut_lease = spawn_ir_future_in_task_group(
                    name="produce", work=produce_handle, task_group=tg
                )
                pfuture, please = _as_handle_future(future).pipeline_dispatch(
                    fut_lease.clone(),
                    hlt.root_lease.clone(),
                    Call(method="m", arg={"x": 1}),
                )
                out = await pfuture.wait(please)
                assert out == {"echoed": "call", "method": "m", "arg": {"x": 1}}
                fut_lease.discard()

    async def test_takes_ownership_of_inputs(self) -> None:
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _EchoHandle(hlt.token, tg)

                async def produce() -> IRValue:
                    return handle

                future, fut_lease = spawn_ir_future_in_task_group(
                    name="produce", work=produce, task_group=tg
                )
                fut_arg = fut_lease.clone()
                inner_arg = hlt.root_lease.clone()
                pfuture, please = _as_handle_future(future).pipeline_dispatch(
                    fut_arg, inner_arg, GetItem(attr="x")
                )
                # Inputs must have been taken synchronously.
                assert not fut_arg.is_active()
                assert not inner_arg.is_active()
                out = await pfuture.wait(please)
                assert out == {"echoed": "getitem", "attr": "x"}
                fut_lease.discard()

    async def test_chained_pipelining(self) -> None:
        """pipeline_dispatch on a future produced by pipeline_dispatch."""
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _EchoHandle(hlt.token, tg)

                # Define a second handle that returns the first handle itself. Also
                # built on `IRHandleLocalImplBase`.
                class _HandleProducer(IRHandleLocalImplBase[Any]):
                    def __init__(
                        self,
                        token: LocalLifetimeToken[Any],
                        task_group: anyio.abc.TaskGroup,
                    ) -> None:
                        self._token = token
                        self._task_group = task_group

                    async def _run_request_async(
                        self,
                        lease: Lease[Any],
                        request: Call[UnwrappedCallArg] | GetItem,
                    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
                        lease.discard()
                        if isinstance(request, Call):
                            discard_leases_in(request.arg)
                        return handle  # hand back the echo handle

                producer = _HandleProducer(hlt.token, tg)

                async def produce_producer() -> IRHandle[Any]:
                    return producer

                # future1 resolves to the producer handle. Because we typed
                # `produce_producer -> IRHandle[Any]`, we get back an
                # `IRFutureLocalImpl[IRHandle[Any], ...]` directly — no cast needed
                # for `pipeline_dispatch`.
                future1, lease1 = spawn_ir_future_in_task_group(
                    name="prod1", work=produce_producer, task_group=tg
                )
                # pipeline_dispatch on future1: the producer handle's request returns
                # the echo handle, so future2 resolves to the echo handle.
                future2, lease2 = future1.pipeline_dispatch(
                    lease1.clone(),
                    hlt.root_lease.clone(),
                    GetItem(attr="anything"),
                )
                # pipeline_dispatch on future2 → dispatches on the echo handle. The
                # future2 here is typed `IRFuture[Any, Any]` (the abstract return type
                # of `pipeline_dispatch`), so we do still need a narrowing cast to use
                # pipeline_dispatch again.
                future3, lease3 = _as_handle_future(future2).pipeline_dispatch(
                    lease2,
                    hlt.root_lease.clone(),
                    Call(method="m2", arg={"v": 2}),
                )
                out = await future3.wait(lease3)
                assert out == {"echoed": "call", "method": "m2", "arg": {"v": 2}}
                lease1.discard()

    async def test_non_handle_resolution_raises(self) -> None:
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:

                async def produce_non_handle() -> IRValue:
                    return 42

                future, fut_lease = spawn_ir_future_in_task_group(
                    name="nonh", work=produce_non_handle, task_group=tg
                )
                pfuture, please = _as_handle_future(future).pipeline_dispatch(
                    fut_lease.clone(),
                    hlt.root_lease.clone(),
                    GetItem(attr="x"),
                )
                with pytest.raises(IRException) as excinfo:
                    await pfuture.wait(please)
                assert excinfo.value.category == "PipelineTypeError"
                assert "not IRHandle" in excinfo.value.message
                fut_lease.discard()

    async def test_unwrap_for_pipelining_lease(self) -> None:
        """inner_handle_lease passed as UnwrapForPipelining → awaits before dispatching."""
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _EchoHandle(hlt.token, tg)

                async def produce_handle() -> IRValue:
                    return handle

                async def produce_lease() -> IRValue:
                    # This "future" resolves to a lease.
                    return hlt.root_lease.clone()

                handle_future, handle_lease = spawn_ir_future_in_task_group(
                    name="h", work=produce_handle, task_group=tg
                )
                lease_future, lease_lease = spawn_ir_future_in_task_group(
                    name="l", work=produce_lease, task_group=tg
                )
                pfuture, please = _as_handle_future(handle_future).pipeline_dispatch(
                    handle_lease.clone(),
                    cast(
                        UnwrapForPipelining[Lease[Any], Any],
                        UnwrapForPipelining(lease_future, lease_lease.clone()),
                    ),
                    Call(method="via-unwrap", arg={}),
                )
                out = await pfuture.wait(please)
                assert out == {"echoed": "call", "method": "via-unwrap", "arg": {}}
                handle_lease.discard()
                lease_lease.discard()

    async def test_non_lease_unwrap_raises(self) -> None:
        """UnwrapForPipelining resolves to something non-Lease → PipelineTypeError."""
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _EchoHandle(hlt.token, tg)

                async def produce_handle() -> IRValue:
                    return handle

                async def produce_not_a_lease() -> IRValue:
                    return 42  # not a Lease

                handle_future, handle_lease = spawn_ir_future_in_task_group(
                    name="h", work=produce_handle, task_group=tg
                )
                not_a_lease_future, nal_lease = spawn_ir_future_in_task_group(
                    name="nal", work=produce_not_a_lease, task_group=tg
                )
                pfuture, please = _as_handle_future(handle_future).pipeline_dispatch(
                    handle_lease.clone(),
                    cast(
                        UnwrapForPipelining[Lease[Any], Any],
                        UnwrapForPipelining(not_a_lease_future, nal_lease.clone()),
                    ),
                    GetItem(attr="x"),
                )
                with pytest.raises(IRException) as excinfo:
                    await pfuture.wait(please)
                assert excinfo.value.category == "PipelineTypeError"
                assert "not Lease" in excinfo.value.message
                handle_lease.discard()
                nal_lease.discard()

    async def test_cleanup_on_early_failure(self) -> None:
        """When the pipelined resolution fails before consuming all inputs, the unused
        leases are still released (not leaked). We verify this by passing in leases
        from a lifetime we control and checking the lifetime still auto-closes."""
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:

                async def produce_non_handle() -> IRValue:
                    return 42

                future, fut_lease = spawn_ir_future_in_task_group(
                    name="x", work=produce_non_handle, task_group=tg
                )
                pfuture, please = _as_handle_future(future).pipeline_dispatch(
                    fut_lease.clone(),
                    hlt.root_lease.clone(),  # inner_handle_lease
                    Call(method="m", arg={"l": hlt.root_lease.clone()}),
                )
                with pytest.raises(IRException):
                    await pfuture.wait(please)
                # The inner_handle_lease and the lease inside arg should have been
                # discarded by the finally clause. Verify by discarding hlt's root
                # and checking the lifetime ends.
                fut_lease.discard()
                hlt.root_lease.discard()
                assert hlt.token.ended()


# ---------------------------------------------------------------------------
# IRHandleLocalImplBase — request / dispatch / _unwrap_request_futures
# ---------------------------------------------------------------------------


class _ForwardingHandle(IRHandleLocalImplBase[Any]):
    """Test handle whose `_run_request_async` returns an `UnwrapForPipelining`, used to
    exercise the forwarding path through dispatch."""

    def __init__(
        self,
        token: LocalLifetimeToken[Any],
        task_group: anyio.abc.TaskGroup,
        forward_to: IRFuture[IRValue, Any],
        forward_lease: Lease[Any],
    ) -> None:
        self._token = token
        self._task_group = task_group
        self._forward_to = forward_to
        self._forward_lease = forward_lease

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[UnwrappedCallArg] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call):
            discard_leases_in(request.arg)
        # Hand ownership of the stored forward lease over; caller is expected to call
        # _run_request_async at most once for this handle.
        return UnwrapForPipelining(self._forward_to, self._forward_lease)


class _RaisingHandle(IRHandleLocalImplBase[Any]):
    """Test handle whose `_run_request_async` raises on demand."""

    def __init__(
        self,
        token: LocalLifetimeToken[Any],
        task_group: anyio.abc.TaskGroup,
        to_raise: BaseException,
    ) -> None:
        self._token = token
        self._task_group = task_group
        self._to_raise = to_raise

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[UnwrappedCallArg] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        if isinstance(request, Call):
            discard_leases_in(request.arg)
        raise self._to_raise


class _CapturingHandle(IRHandleLocalImplBase[Any]):
    """Test handle that captures the (unwrapped) request it receives for inspection —
    useful for verifying `_unwrap_request_futures` semantics."""

    def __init__(self, token: LocalLifetimeToken[Any], task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group
        self.captured: list[Call[UnwrappedCallArg] | GetItem] = []

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[UnwrappedCallArg] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        lease.discard()
        self.captured.append(request)
        if isinstance(request, Call):
            discard_leases_in(request.arg)
        return cast(IRValue, None)


class TestHandleBaseRequest:
    async def test_basic_request(self) -> None:
        async with open_lifetime_task_group("t") as (lt, tg):
            handle = _EchoHandle(lt.token, tg)
            out = await handle.request(lt.root_lease.clone(), Call(method="m", arg={"x": 1}))
            assert out == {"echoed": "call", "method": "m", "arg": {"x": 1}}
            lt.root_lease.discard()

    async def test_request_discards_inputs(self) -> None:
        """request takes ownership of the lease and the request's leases."""
        async with open_lifetime_task_group("t") as (lt, tg):
            handle = _EchoHandle(lt.token, tg)
            lease_arg = lt.root_lease.clone()
            passed_lease = lt.root_lease.clone()
            # The echo hands our input leases back inside the result; discard them.
            result = await handle.request(passed_lease, Call(method="m", arg={"inner": lease_arg}))
            discard_leases_in(result)
            # Both input leases consumed by the request.
            assert not passed_lease.is_active()
            assert not lease_arg.is_active()
            lt.root_lease.discard()

    async def test_request_ir_exception_passthrough(self) -> None:
        async with open_lifetime_task_group("t") as (lt, tg):
            handle = _RaisingHandle(
                lt.token, tg, IRException("oops", category="MyCat", data={"x": 1})
            )
            with pytest.raises(IRException) as excinfo:
                await handle.request(lt.root_lease.clone(), GetItem(attr="x"))
            assert excinfo.value.message == "oops"
            assert excinfo.value.category == "MyCat"
            assert excinfo.value.data == {"x": 1}
            lt.root_lease.discard()

    async def test_request_plain_exception_wraps(self) -> None:
        async with open_lifetime_task_group("t") as (lt, tg):
            handle = _RaisingHandle(lt.token, tg, ValueError("bad"))
            with pytest.raises(IRException) as excinfo:
                await handle.request(lt.root_lease.clone(), GetItem(attr="x"))
            assert excinfo.value.message == "bad"
            assert excinfo.value.category == "ValueError"
            lt.root_lease.discard()

    async def test_request_forwarding_path(self) -> None:
        """If `_run_request_async` returns an `UnwrapForPipelining`, request resolves to
        the forwarded future's value."""
        async with open_lifetime_task_group("t") as (lt, tg):

            async def inner_work() -> IRValue:
                return {"from": "forwarded"}

            inner_future, inner_lease = spawn_ir_future_in_task_group(
                name="inner", work=inner_work, task_group=tg
            )
            handle = _ForwardingHandle(lt.token, tg, inner_future, inner_lease)
            out = await handle.request(lt.root_lease.clone(), GetItem(attr="x"))
            assert out == {"from": "forwarded"}
            lt.root_lease.discard()


class TestHandleBaseDispatch:
    async def test_dispatch_returns_future_and_lease(self) -> None:
        async with open_lifetime_task_group("t") as (lt, tg):
            handle = _EchoHandle(lt.token, tg)
            future, result_lease = handle.dispatch(
                lt.root_lease.clone(), Call(method="m", arg=[1, 2])
            )
            assert result_lease.is_active()
            out = await future.wait(result_lease)
            assert out == {"echoed": "call", "method": "m", "arg": [1, 2]}
            lt.root_lease.discard()

    async def test_dispatch_takes_inputs_synchronously(self) -> None:
        async with open_lifetime_task_group("t") as (lt, tg):
            handle = _EchoHandle(lt.token, tg)
            lease_in_arg = lt.root_lease.clone()
            lease_passed = lt.root_lease.clone()
            future, result_lease = handle.dispatch(lease_passed, Call(method="m", arg=lease_in_arg))
            # Synchronously: both input leases are consumed.
            assert not lease_passed.is_active()
            assert not lease_in_arg.is_active()
            # Echo returns the args in its result; discard the leases inside.
            result = await future.wait(result_lease)
            discard_leases_in(result)
            lt.root_lease.discard()

    async def test_dispatch_wait_cancel_does_not_cancel_work(self) -> None:
        """Because `request` is implemented as `dispatch + wait`, abandoning the wait
        should not cancel the underlying dispatched work."""
        work_completed = anyio.Event()

        async with open_lifetime_task_group("t") as (lt, tg):

            class _SlowHandle(IRHandleLocalImplBase[Any]):
                def __init__(
                    self,
                    token: LocalLifetimeToken[Any],
                    task_group: anyio.abc.TaskGroup,
                ) -> None:
                    self._token = token
                    self._task_group = task_group

                async def _run_request_async(
                    self,
                    lease: Lease[Any],
                    request: Call[UnwrappedCallArg] | GetItem,
                ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
                    lease.discard()
                    if isinstance(request, Call):
                        discard_leases_in(request.arg)
                    try:
                        await anyio.sleep(0.05)
                        return cast(IRValue, "done")
                    finally:
                        work_completed.set()

            handle = _SlowHandle(lt.token, tg)
            future, result_lease = handle.dispatch(lt.root_lease.clone(), GetItem(attr="x"))
            # Cancel the wait before the work completes.
            with anyio.move_on_after(0.01):
                await future.wait(result_lease.clone())
            # The work is still running; wait for it to finish.
            await work_completed.wait()
            result_lease.discard()
            lt.root_lease.discard()

    async def test_dispatch_forwarding_return(self) -> None:
        """When `_run_request_async` returns `UnwrapForPipelining`, the result box
        forwards to that future rather than re-boxing the value."""
        async with open_lifetime_task_group("t") as (lt, tg):

            async def inner_work() -> IRValue:
                return 77

            inner_future, inner_lease = spawn_ir_future_in_task_group(
                name="inner", work=inner_work, task_group=tg
            )
            handle = _ForwardingHandle(lt.token, tg, inner_future, inner_lease)
            future, result_lease = handle.dispatch(lt.root_lease.clone(), GetItem(attr="anything"))
            assert await future.wait(result_lease) == 77
            lt.root_lease.discard()


class TestUnwrapRequestFutures:
    async def test_getitem_passthrough(self) -> None:
        async with open_lifetime_task_group("t") as (lt, tg):
            handle = _CapturingHandle(lt.token, tg)
            await handle.request(lt.root_lease.clone(), GetItem(attr="z"))
            assert len(handle.captured) == 1
            got = handle.captured[0]
            assert isinstance(got, GetItem)
            assert got.attr == "z"
            lt.root_lease.discard()

    async def test_unwrap_in_call_arg(self) -> None:
        """An `UnwrapForPipelining` inside `Call.arg` is awaited + substituted before
        reaching `_run_request_async`."""
        async with open_lifetime_task_group("t") as (lt, tg):

            async def produce_value() -> IRValue:
                return "resolved-async"

            inner_future, inner_lease = spawn_ir_future_in_task_group(
                name="inner", work=produce_value, task_group=tg
            )
            handle = _CapturingHandle(lt.token, tg)
            await handle.request(
                lt.root_lease.clone(),
                Call(
                    method="m",
                    arg={"from_future": UnwrapForPipelining(inner_future, inner_lease)},
                ),
            )
            assert len(handle.captured) == 1
            got = handle.captured[0]
            assert isinstance(got, Call)
            got_arg = cast(dict[str, Any], got.arg)
            assert got_arg["from_future"] == "resolved-async"
            lt.root_lease.discard()

    async def test_wait_and_ignore_single(self) -> None:
        """WaitAndIgnoreFutureThen(single, then) waits for the single future, then runs
        the inner request."""
        side_effects: list[str] = []
        async with open_lifetime_task_group("t") as (lt, tg):

            async def marker() -> IRValue:
                side_effects.append("marker-ran")
                return "ignored-result"

            inner_future, inner_lease = spawn_ir_future_in_task_group(
                name="m", work=marker, task_group=tg
            )
            handle = _CapturingHandle(lt.token, tg)
            await handle.request(
                lt.root_lease.clone(),
                WaitAndIgnoreFutureThen(
                    wait_for=UnwrapForPipelining(inner_future, inner_lease),
                    then=GetItem(attr="after"),
                ),
            )
            assert side_effects == ["marker-ran"]
            assert len(handle.captured) == 1
            got = handle.captured[0]
            assert isinstance(got, GetItem)
            assert got.attr == "after"
            lt.root_lease.discard()

    async def test_wait_and_ignore_sequence(self) -> None:
        """wait_for as a sequence — all awaited before `then` runs."""
        side_effects: list[str] = []
        async with open_lifetime_task_group("t") as (lt, tg):

            async def make_marker(name: str) -> Any:
                async def work() -> IRValue:
                    await anyio.sleep(0.01)
                    side_effects.append(name)
                    return name

                return spawn_ir_future_in_task_group(name=name, work=work, task_group=tg)

            f1, l1 = await make_marker("a")
            f2, l2 = await make_marker("b")
            handle = _CapturingHandle(lt.token, tg)
            await handle.request(
                lt.root_lease.clone(),
                WaitAndIgnoreFutureThen(
                    wait_for=[
                        UnwrapForPipelining(f1, l1),
                        UnwrapForPipelining(f2, l2),
                    ],
                    then=GetItem(attr="after"),
                ),
            )
            # Both markers ran before the inner request.
            assert set(side_effects) == {"a", "b"}
            assert len(handle.captured) == 1
            got = handle.captured[0]
            assert isinstance(got, GetItem)
            lt.root_lease.discard()

    async def test_nested_wait_and_ignore(self) -> None:
        """WaitAndIgnoreFutureThen's `then` can itself be another
        WaitAndIgnoreFutureThen."""
        side_effects: list[str] = []
        async with open_lifetime_task_group("t") as (lt, tg):

            async def marker(name: str) -> IRValue:
                side_effects.append(name)
                return name

            f_outer, l_outer = spawn_ir_future_in_task_group(
                name="outer", work=lambda: marker("outer"), task_group=tg
            )
            f_inner, l_inner = spawn_ir_future_in_task_group(
                name="inner", work=lambda: marker("inner"), task_group=tg
            )
            handle = _CapturingHandle(lt.token, tg)
            await handle.request(
                lt.root_lease.clone(),
                WaitAndIgnoreFutureThen(
                    wait_for=UnwrapForPipelining(f_outer, l_outer),
                    then=WaitAndIgnoreFutureThen(
                        wait_for=UnwrapForPipelining(f_inner, l_inner),
                        then=GetItem(attr="final"),
                    ),
                ),
            )
            # Outer ran first, then inner, then the GetItem was captured.
            assert side_effects == ["outer", "inner"]
            assert len(handle.captured) == 1
            assert isinstance(handle.captured[0], GetItem)
            lt.root_lease.discard()


# ---------------------------------------------------------------------------
# Pipelined outer-lease form for IRHandle.dispatch / IRHandle.request
# (substage 4 — UnwrapForPipelining[Lease[B], Any] in the lease slot).
# ---------------------------------------------------------------------------


class _LeaseRecorder(IRHandleLocalImplBase[Any]):
    """Test handle that records the grant of every lease it receives in
    ``_run_request_async``. Discards both the lease and any leases inside the
    request per the transfer convention.
    """

    def __init__(self, token: LocalLifetimeToken[Any], task_group: anyio.abc.TaskGroup) -> None:
        self._token = token
        self._task_group = task_group
        self.received_grants: list[Any] = []

    async def _run_request_async(
        self,
        lease: Lease[Any],
        request: Call[UnwrappedCallArg] | GetItem,
    ) -> IRValue | UnwrapForPipelining[IRValue, Any]:
        self.received_grants.append(lease.grant)
        lease.discard()
        if isinstance(request, Call):
            discard_leases_in(request.arg)
        return cast(IRValue, "ok")


class TestDispatchPipelinedLease:
    """Substage 4: outer dispatch lease may be `Lease | UnwrapForPipelining[Lease, ...]`."""

    async def test_concrete_lease_still_works(self) -> None:
        """Regression: dispatch with a plain Lease still works after the widening."""
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _LeaseRecorder(hlt.token, tg)
                future, result_lease = handle.dispatch(hlt.root_lease.clone(), GetItem(attr="x"))
                out = await future.wait(result_lease)
                assert out == "ok"
                assert len(handle.received_grants) == 1
                assert handle.received_grants[0] == hlt.root_lease.grant

    async def test_pipelined_lease_resolves_inside_runner(self) -> None:
        """Outer lease as UnwrapForPipelining → runner awaits the future to get the
        concrete Lease before invoking _run_request_async, then dispatches normally.
        """
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _LeaseRecorder(hlt.token, tg)
                # Build a future-of-lease — a future whose resolved value is a Lease[B].
                # The future has its own (separate) lifetime; its result lease is the
                # actual handle-lease.
                lease_to_return = hlt.root_lease.clone()

                async def produce_lease() -> IRValue:
                    return lease_to_return

                lease_future, lease_future_lease = spawn_ir_future_in_task_group(
                    name="produce_lease", work=produce_lease, task_group=tg
                )
                future, result_lease = handle.dispatch(
                    cast(
                        "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                        UnwrapForPipelining(lease_future, lease_future_lease),
                    ),
                    Call(method="m", arg={"x": 1}),
                )
                out = await future.wait(result_lease)
                assert out == "ok"
                assert len(handle.received_grants) == 1
                assert handle.received_grants[0] == hlt.root_lease.grant

    async def test_pipelined_lease_type_mismatch_raises(self) -> None:
        """The future for the outer lease resolves to a non-Lease →
        IRException(category="PipelineTypeError"); _run_request_async never called.
        """
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _LeaseRecorder(hlt.token, tg)

                async def produce_non_lease() -> IRValue:
                    return 42  # not a Lease

                lease_future, lease_future_lease = spawn_ir_future_in_task_group(
                    name="bad", work=produce_non_lease, task_group=tg
                )
                future, result_lease = handle.dispatch(
                    cast(
                        "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                        UnwrapForPipelining(lease_future, lease_future_lease),
                    ),
                    GetItem(attr="x"),
                )
                with pytest.raises(IRException) as excinfo:
                    await future.wait(result_lease)
                assert excinfo.value.category == "PipelineTypeError"
                assert "not Lease" in excinfo.value.message
                # _run_request_async should never have been called.
                assert handle.received_grants == []

    async def test_pipelined_lease_future_rejects(self) -> None:
        """The future for the outer lease itself rejects → the dispatched future
        rejects with the same IRException (wrapped/passed through by the runner's
        outcome routing). _run_request_async never called.
        """
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _LeaseRecorder(hlt.token, tg)

                async def producer_that_fails() -> IRValue:
                    raise IRException(message="upstream failed", category="UpstreamRejected")

                lease_future, lease_future_lease = spawn_ir_future_in_task_group(
                    name="reject", work=producer_that_fails, task_group=tg
                )
                future, result_lease = handle.dispatch(
                    cast(
                        "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                        UnwrapForPipelining(lease_future, lease_future_lease),
                    ),
                    GetItem(attr="x"),
                )
                with pytest.raises(IRException) as excinfo:
                    await future.wait(result_lease)
                # IRException is preserved as-is via the runner's `except IRException`
                # branch; no PipelineTypeError wrapper.
                assert excinfo.value.category == "UpstreamRejected"
                assert handle.received_grants == []

    async def test_pipelined_lease_source_revoked(self) -> None:
        """The future-of-lease's lifetime is revoked while the dispatch runner is
        awaiting it → the inner `wait` raises `LeaseBrokenError`, which the runner's
        outcome routing wraps as `IRException(category="LeaseBrokenError")`.
        `_run_request_async` is never called.

        We construct the future-of-lease manually (rather than via
        `spawn_ir_future_in_task_group`) so we have a handle on its lifetime and can
        revoke it externally mid-wait.
        """
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _LeaseRecorder(hlt.token, tg)
                # Future-of-lease backed by a ULL we control (`src_lt`). The box
                # stays in `pending` state until we revoke `src_lt`, at which point
                # the box's token-end path transitions it to `discarded` and any
                # in-flight `wait` raises LeaseBrokenError.
                src_lt = UnconditionalLocalLifetime("future-source")
                box: LocalPendingResultBox[Any, Any] = LocalPendingResultBox(src_lt.token)
                lease_future = IRFutureLocalImpl[Any, Any](
                    box,
                    attr_path=(),
                    task_group=tg,
                    expected_grant=src_lt.root_lease.grant,
                )
                src_lease_for_unwrap = src_lt.root_lease.clone()
                future, result_lease = handle.dispatch(
                    cast(
                        "Lease[Any] | UnwrapForPipelining[Lease[Any], Any]",
                        UnwrapForPipelining(lease_future, src_lease_for_unwrap),
                    ),
                    GetItem(attr="x"),
                )
                # Yield once so the runner gets to call `_resolve_pipelined_lease`
                # and start awaiting the box. Then revoke the future's lifetime to
                # break the in-flight wait.
                await anyio.sleep(0)
                src_lt.revoke("test-revoke")
                with pytest.raises(IRException) as excinfo:
                    await future.wait(result_lease)
                # `LeaseBrokenError` (a regular Exception) is wrapped by
                # `spawn_ir_future_in_task_group`'s `except Exception` branch into
                # `IRException(message=str(exc), category=type(exc).__name__)`.
                assert excinfo.value.category == "LeaseBrokenError"
                assert handle.received_grants == []

    async def test_unwrap_request_futures_failure_cleans_up(self) -> None:
        """Pre-existing leak fix: if `_unwrap_request_futures` raises (e.g. a
        WaitAndIgnoreFutureThen.wait_for future rejects), unconsumed leases inside
        the taken request are now discarded by the runner's cleanup-on-failure
        block. Verify by checking lifetime auto-closes after the dispatch fails.
        """
        with UnconditionalLocalLifetime("hlt").revoke_after_block() as hlt:
            async with anyio.create_task_group() as tg:
                handle = _LeaseRecorder(hlt.token, tg)
                # Use a separate, drainable lifetime for a lease we'll embed in a
                # call arg that should be cleaned up if the dispatch fails before
                # _run_request_async runs.
                async with UnconditionalLocalLifetime("inner") as inner_lt:

                    async def rejecting_producer() -> IRValue:
                        raise IRException(message="boom", category="WaitForRejected")

                    rej_future, rej_lease = spawn_ir_future_in_task_group(
                        name="rej", work=rejecting_producer, task_group=tg
                    )
                    # Dispatch with a `WaitAndIgnoreFutureThen` whose wait_for
                    # future rejects. Embed a lease from `inner_lt` inside the
                    # `then` request so we can detect whether it was leaked.
                    future, result_lease = handle.dispatch(
                        hlt.root_lease.clone(),
                        WaitAndIgnoreFutureThen(
                            wait_for=UnwrapForPipelining(rej_future, rej_lease),
                            then=Call(method="m", arg={"embedded": inner_lt.root_lease.clone()}),
                        ),
                    )
                    with pytest.raises(IRException) as excinfo:
                        await future.wait(result_lease)
                    assert excinfo.value.category == "WaitForRejected"
                    assert handle.received_grants == []
                    # `inner_lt`'s root lease was discarded in the cleanup branch
                    # — the `async with` exit should drain promptly without
                    # hanging on the embedded lease.


# (TestOpenLifetimeTaskGroup moved to `test_lease_util.py` alongside `lease_cancel_scope`
# tests, since both helpers live in `fxtr2/leases/lease_util.py`.)
