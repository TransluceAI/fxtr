"""Tests for the per-call input scope (stage 12 post 1, ``input_scope_plan.md``).

Covers the new ``_fxtr_get_input_scope()`` surface and the lifetime
properties that motivated the split: retaining the input scope keeps
the call's data loadable past the dispatch *without pinning the mount
token* (the headline — the mount drains and tears down with a
retention outstanding), while what it does pin is the caller side
(the arg senders), released with the retention or broken by the
caller's death. Also: the unretained input scope closes with the
call, store-into works, and the accessor errors outside a dispatch.

The two-hop arg-access path (call scope → input-scope parent →
arg token) and the chain-converter return path are regression-covered
by the existing service suites (notably ``test_untyped_service``'s
spawner-pattern and layout-parametrized tests); this file focuses on
the new behaviors.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

import anyio
import anyio.abc
import pytest

from fxtr2.conversion.converters.mashumaro_entities import DataclassEntity, EntityRef
from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, get_active_scope
from fxtr2.leases.lease_types import Lease
from fxtr2.services.untyped_service import UntypedServiceImpl, service_method

pytestmark = pytest.mark.anyio

_INPUT_SCOPE_TEST_NS = uuid.UUID("5b1f7c3e-9d24-4a8b-b1c6-7e2f90d4a513")


@dataclasses.dataclass(frozen=True)
class _Note(DataclassEntity, fxtr_type=system_entities.define_type(_INPUT_SCOPE_TEST_NS, "note")):
    text: str


class _ProbeImpl(UntypedServiceImpl):
    """Records / retains its calls' input scopes (test-only: stashing on
    ``self`` is fine here, the tests own the instance)."""

    def __init__(self) -> None:
        super().__init__()
        self.seen_input_scopes: list[FxtrScope] = []
        self.retained: list[tuple[FxtrScope, Lease[Any]]] = []

    def release_retained(self) -> None:
        for _, lease in self.retained:
            lease.discard()
        self.retained.clear()

    @service_method
    async def peek(self) -> None:
        """Record the call's input scope without retaining it."""
        self.seen_input_scopes.append(self._fxtr_get_input_scope())

    @service_method
    async def grab(self, note_id: FxtrID) -> str:
        """Load the arg (from the call scope, through the input-scope
        parent hop) and retain the input scope."""
        scope = self._fxtr_get_input_scope()
        self.seen_input_scopes.append(scope)
        self.retained.append((scope, scope.clone_authorization()))
        loaded = await EntityRef(entity_id=note_id, target_type=_Note).load()
        return loaded.text

    @service_method
    async def stash(self, text: str) -> str:
        """Store a fresh entity *into* the input scope and retain it."""
        scope = self._fxtr_get_input_scope()
        ref = _Note(text=text).store(scope)
        self.retained.append((scope, scope.clone_authorization()))
        return ref.entity_id.id_string()

    @service_method
    async def active_is_not_input_scope(self) -> bool:
        return get_active_scope() is not self._fxtr_get_input_scope()


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.01)


async def _settle() -> None:
    await anyio.sleep(0.1)


async def _work_holder(
    release: anyio.Event,
    *,
    task_status: anyio.abc.TaskStatus[FxtrScope] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Holds the caller-side work scope open until ``release`` fires;
    its CM exit then parks in the drain wait for as long as anything
    (e.g. a retained input scope) pins the work token."""
    async with FxtrScope(name="input_scope_test_work", include=[GLOBAL_SCOPE], isolated=True) as s:
        task_status.started(s)
        await release.wait()


def test_accessor_outside_dispatch_raises() -> None:
    impl = _ProbeImpl()
    with pytest.raises(RuntimeError, match="outside a service dispatch"):
        impl._fxtr_get_input_scope()


async def test_unretained_input_scope_closes_with_the_call() -> None:
    impl = _ProbeImpl()
    async with FxtrScope(name="mount", include=[GLOBAL_SCOPE], isolated=True) as mount:
        ref = await mount.mount(impl)
        async with FxtrScope(name="caller", include=[GLOBAL_SCOPE], isolated=True) as caller:
            await caller.bridge_service_from_scope(ref, mount, keepalive_via="independent_lease")
            proxy = await ref.resolve(caller)
            await proxy.peek()
            assert await proxy.active_is_not_input_scope() is True
            (scope,) = impl.seen_input_scopes
            await _wait_until(lambda: not scope.is_open())
            caller.forget_service(ref)


async def test_retention_survives_dispatch_and_mount_drains() -> None:
    """The headline: an impl retains its input scope; the mount still
    drains + tears down with the retention outstanding; the arg stays
    loadable through the retained lease; releasing it closes the input
    scope (and unpins the caller)."""
    impl = _ProbeImpl()
    async with anyio.create_task_group() as tg:
        release = anyio.Event()
        work = await tg.start(_work_holder, release)
        note_id = _Note(text="hello").store(work).entity_id

        with anyio.fail_after(10):
            mount_drained = anyio.Event()

            async def _host(
                *,
                task_status: anyio.abc.TaskStatus[Any] = anyio.TASK_STATUS_IGNORED,
            ) -> None:
                async with FxtrScope(name="mount", include=[GLOBAL_SCOPE], isolated=True) as mount:
                    ref = await mount.mount(impl)
                    await work.bridge_service_from_scope(
                        ref, mount, keepalive_via="independent_lease"
                    )
                    task_status.started(ref)
                # CM exit parks in the mount's drain wait.
                mount_drained.set()

            ref = await tg.start(_host)
            proxy = await ref.resolve(work)
            assert await proxy.grab(note_id) == "hello"
            assert len(impl.retained) == 1

            # Drop the last service-ref holder: the mount must drain
            # even though the input-scope retention is still out.
            work.forget_service(ref)
            await mount_drained.wait()

        (scope, lease) = impl.retained[0]
        assert scope.is_open()  # drained-but-open under the retention
        with scope.authorize(lease.clone()):
            loaded = await EntityRef(entity_id=note_id, target_type=_Note).load(scope)
        assert loaded.text == "hello"

        # The retention pins the *caller* (the work scope sent the
        # arg): ask the holder to exit — it must park in the drain
        # wait until the impl releases.
        release.set()
        await _settle()
        assert work.is_open()
        impl.release_retained()
        await _wait_until(lambda: not scope.is_open())
        await _wait_until(lambda: not work.is_open())


async def test_store_into_input_scope_survives_dispatch() -> None:
    impl = _ProbeImpl()
    async with anyio.create_task_group() as tg:
        release = anyio.Event()
        work = await tg.start(_work_holder, release)
        async with FxtrScope(name="mount", include=[GLOBAL_SCOPE], isolated=True) as mount:
            ref = await mount.mount(impl)
            await work.bridge_service_from_scope(ref, mount, keepalive_via="independent_lease")
            proxy = await ref.resolve(work)
            id_string = await proxy.stash("stashed")
            (scope, lease) = impl.retained[0]
            stored_id = FxtrID.from_id_string(id_string)
            with scope.authorize(lease.clone()):
                loaded = await EntityRef(entity_id=stored_id, target_type=_Note).load(scope)
            assert loaded.text == "stashed"
            impl.release_retained()
            await _wait_until(lambda: not scope.is_open())
            work.forget_service(ref)
        release.set()


async def test_caller_death_breaks_retained_input_scope() -> None:
    """Force-closing the caller (its token revokes) cascades into the
    retained input scope rather than leaking it."""
    impl = _ProbeImpl()
    async with anyio.create_task_group() as tg:
        release = anyio.Event()
        work = await tg.start(_work_holder, release)
        note_id = _Note(text="doomed").store(work).entity_id
        async with FxtrScope(name="mount", include=[GLOBAL_SCOPE], isolated=True) as mount:
            ref = await mount.mount(impl)
            await work.bridge_service_from_scope(ref, mount, keepalive_via="independent_lease")
            proxy = await ref.resolve(work)
            assert await proxy.grab(note_id) == "doomed"
            (scope, _) = impl.retained[0]
            assert scope.is_open()

            work.force_close()  # the caller dies without releasing anything
            await _wait_until(lambda: not scope.is_open())
            impl.release_retained()  # discarding the broken lease is fine
        release.set()
