"""Tests for the scope-drain report (`fxtr2/debug/scope_report.py`) and its
always-on instrumentation (`fxtr2/debug/scope_instrumentation.py`)."""

from __future__ import annotations

import gc
import os
import signal
import uuid
from typing import Any, Protocol

import anyio
import anyio.abc
import pytest

from fxtr2.data import system_entities
from fxtr2.data.entity_core import FxtrID
from fxtr2.data.entity_ref_scope import GLOBAL_SCOPE, FxtrScope, get_active_scope
from fxtr2.debug import scope_instrumentation as inst
from fxtr2.debug.scope_report import (
    build_scope_report,
    dump_scope_report,
    fail_after_and_report_hung_scopes,
    report_hung_scopes_on_signal,
)
from fxtr2.leases import local_lease
from fxtr2.services.typed_service import TypedServiceImpl, service_protocol
from fxtr2.services.untyped_service import service_method

pytestmark = pytest.mark.anyio

_NS = uuid.UUID("aa11c0de-5c0e-4e08-9a41-000000000001")

# Module-import-time on purpose: ``define_type`` always stores into
# GLOBAL_SCOPE, and the conftest's leak guard snapshots GLOBAL per test.
_PROBE_TYPE = system_entities.define_type(_NS, "scope_report_probe")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@service_protocol
class ProbeProto(Protocol):
    async def probe(self) -> bool: ...


class ProbeImpl(TypedServiceImpl[ProbeProto]):
    """Reports whether its dispatch ran on a flagged per-call scope."""

    def _target_type_annotation(self) -> Any:
        return ProbeProto

    def _service_type_id(self) -> FxtrID:
        return _PROBE_TYPE

    @service_method
    async def probe(self) -> bool:
        return get_active_scope()._is_dispatch_call_scope


# ---------------------------------------------------------------------------
# The synthetic wedge: a scope draining while a fork-inherited implicit child
# keeps it parked — the failure class the report exists to name.
# ---------------------------------------------------------------------------


class _Wedge:
    def __init__(self, parent: FxtrScope) -> None:
        self.parent = parent
        self.entered = anyio.Event()
        self.release = anyio.Event()
        self.drained = anyio.Event()

    async def _holder(self) -> None:
        # Inherits the starting context (ACTIVE_SCOPE = parent) — the
        # non-isolated child then captures the parent cross-task.
        async with FxtrScope(name="wedge-child"):
            self.entered.set()
            await self.release.wait()

    async def _closer(self) -> None:
        await self.parent.aclose()
        self.drained.set()

    async def start(self, tg: anyio.abc.TaskGroup) -> None:
        with self.parent.as_active_scope():
            tg.start_soon(self._holder)
        await self.entered.wait()
        tg.start_soon(self._closer)
        # Wait until the drain actually parks (timestamp set by aclose).
        while self.parent._drain_started_at is None:
            await anyio.sleep(0.01)

    async def unwedge(self) -> None:
        self.release.set()
        await self.drained.wait()


def _make_parent(name: str = "wedge-parent") -> FxtrScope:
    parent = FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name=name)
    parent.lowlevel_start_lifespanless(
        parent_leases={GLOBAL_SCOPE: GLOBAL_SCOPE.clone_authorization()}
    )
    return parent


async def test_wedged_report_names_blocker_edge_and_hazard() -> None:
    wedge = _Wedge(_make_parent())
    report = ""
    async with anyio.create_task_group() as tg:
        await wedge.start(tg)
        report = build_scope_report("test trigger")
        await wedge.unwedge()
    assert "test trigger" in report
    assert "'wedge-parent'" in report and "parked" in report
    assert "parent-via lease held by scope 'wedge-child'" in report
    assert "IMPLICIT active-scope capture, CROSS-TASK" in report
    assert "!! hazard" in report
    assert "Live-scope inventory" in report
    # Creation sites point at this file, not at the scope module.
    assert __file__.rsplit("/", 1)[-1] in report


async def test_registry_drops_closed_scopes() -> None:
    async with FxtrScope(isolated=True, include=[GLOBAL_SCOPE], name="registry-probe"):
        assert "'registry-probe'" in build_scope_report("t")
    gc.collect()
    assert "'registry-probe'" not in build_scope_report("t")


async def test_edge_classification() -> None:
    parent = _make_parent("edge-parent")
    try:
        # Same-task implicit capture: not cross-task.
        with parent.as_active_scope():
            same_task_child = FxtrScope(name="same-task-child")
        assert same_task_child._outer_scope is parent
        assert same_task_child._implicit_capture_cross_task is False

        # Explicit include: no implicit edge at all.
        explicit_child = FxtrScope(isolated=True, include=[parent], name="explicit-child")
        assert explicit_child._outer_scope is None
        assert explicit_child._implicit_capture_cross_task is False

        # Fork-inherited implicit capture: cross-task.
        captured: dict[str, bool] = {}
        started = anyio.Event()

        async def forked() -> None:
            child = FxtrScope(name="forked-child")
            captured["cross"] = child._implicit_capture_cross_task
            assert child._outer_scope is parent
            started.set()

        async with anyio.create_task_group() as tg:
            with parent.as_active_scope():
                tg.start_soon(forked)
            await started.wait()
        assert captured["cross"] is True
    finally:
        await parent.aclose()


async def test_dispatch_call_scope_is_flagged(local_fxtr_scope: FxtrScope) -> None:
    async with FxtrScope(isolated=True, include=[local_fxtr_scope], name="mount-scope") as mount:
        ref = await mount.mount(ProbeImpl())
        proxy = await ref.resolve(mount)
        assert await proxy.probe() is True
        # And this test's own scopes are not flagged.
        assert mount._is_dispatch_call_scope is False


async def test_independent_lease_bridge_gets_holder_label(
    local_fxtr_scope: FxtrScope,
) -> None:
    """A scope's drain parked on a bridge lease renders the lease's holder
    label in the report (the shape of the sandbox-launcher deadlock's
    H-lease — see the extra8 plan)."""
    dest_holder: dict[str, FxtrScope] = {}
    dest_ready = anyio.Event()
    dest_release = anyio.Event()
    bridged = anyio.Event()
    drained = anyio.Event()

    async def run_dest() -> None:
        async with FxtrScope(isolated=True, include=[local_fxtr_scope], name="label-dest") as dest:
            dest_holder["dest"] = dest
            dest_ready.set()
            await dest_release.wait()

    async def run_source() -> None:
        async with FxtrScope(
            isolated=True, include=[local_fxtr_scope], name="label-source"
        ) as source:
            ref = await source.mount(ProbeImpl())
            await dest_holder["dest"].bridge_service_from_scope(
                ref, source, keepalive_via="independent_lease"
            )
            bridged.set()
        # The CM exit parks in aclose until dest releases the bridge lease.
        drained.set()

    report = ""
    async with anyio.create_task_group() as tg:
        tg.start_soon(run_dest)
        await dest_ready.wait()
        tg.start_soon(run_source)
        await bridged.wait()
        for _ in range(200):
            await anyio.sleep(0.01)
            report = build_scope_report("t")
            if "'label-source'" in report and "independent_lease into label-dest" in report:
                break
        else:
            pytest.fail("wedged source never rendered its labeled bridge lease")
        dest_release.set()
        await drained.wait()


async def test_guard_times_out_and_reports_wedge(capsys: pytest.CaptureFixture[str]) -> None:
    wedge = _Wedge(_make_parent("guard-wedge-parent"))
    async with anyio.create_task_group() as tg:
        await wedge.start(tg)
        with pytest.raises(TimeoutError):
            async with fail_after_and_report_hung_scopes(0.1):
                await anyio.sleep(5)
        await wedge.unwedge()
    err = capsys.readouterr().err
    assert "fxtr2 scope-drain report" in err
    assert "guard deadline (0.1s) reached" in err
    assert "'guard-wedge-parent'" in err


async def test_guard_no_wedge_prints_one_liner(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(TimeoutError):
        async with fail_after_and_report_hung_scopes(0.05):
            await anyio.sleep(5)
    err = capsys.readouterr().err
    assert "no parked scope drains" in err
    assert "fxtr2 scope-drain report" not in err


async def test_guard_report_false_and_success_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(TimeoutError):
        async with fail_after_and_report_hung_scopes(0.05, report=False):
            await anyio.sleep(5)
    async with fail_after_and_report_hung_scopes(5):
        await anyio.sleep(0.01)
    assert capsys.readouterr().err == ""


async def test_signal_cm_dumps_on_delivery(capsys: pytest.CaptureFixture[str]) -> None:
    async with report_hung_scopes_on_signal():
        os.kill(os.getpid(), signal.SIGUSR1)
        for _ in range(100):
            await anyio.sleep(0.01)
            if "fxtr2 scope-drain report" in capsys.readouterr().err:
                break
        else:
            pytest.fail("signal-triggered report never arrived")


async def test_tb_flag_adds_tracebacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inst, "CAPTURE_CREATION_TB", True)
    monkeypatch.setattr(local_lease, "LEASE_LEAK_TRACK_ALLOCATIONS", True)
    wedge = _Wedge(_make_parent("tb-parent"))
    report = ""
    async with anyio.create_task_group() as tg:
        await wedge.start(tg)
        report = build_scope_report("t")
        await wedge.unwedge()
    assert "creation traceback (tail):" in report
    assert "allocated at (tail):" in report
    assert "rerun with FXTR_SCOPE_REPORT_TB=1" not in report


async def test_manual_dump_writes_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    dump_scope_report("manual test")
    err = capsys.readouterr().err
    assert "fxtr2 scope-drain report" in err
    assert "manual test" in err
    assert "No scope drains are currently parked." in err
