"""Headless smoke for the demo's host core: the supervisor-owned runner
mount + the driver's scenario arcs, through a real resolved proxy (no
workstation).

Deliberately light (the browser drive is the chunk's real test): one
arc test covering pipeline / staleness / flaky-retry / boundness, the
in-process crash → revive → resume arc, and a full-remount test
covering the persistence + auto-bind promise (state built in one mount
is served — not re-run — by the next).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import anyio
import pytest

import demo_task_fns
from experiment_demo_helper import ExperimentDemoHelperProto
from experiment_demo_helper_service import mount_experiment_demo_helper
from experiment_runner import RunnerSupervisor

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def _mounted(data_dir: Path) -> AsyncIterator[tuple[RunnerSupervisor, Any]]:
    """A live supervisor (runner up, no workstation registrar) + a
    resolved driver proxy."""
    async with RunnerSupervisor(data_dir) as sup:
        await sup.revive()
        async with mount_experiment_demo_helper(sup) as mounted:
            ref = mounted.service_ref.replace_service_type(ExperimentDemoHelperProto)
            yield sup, await ref.resolve(mounted.scope)


def _fast_slow(monkeypatch: pytest.MonkeyPatch, *, steps: int, seconds: float) -> None:
    """Shrink the slow task to test size (a demo knob, not a timing
    assertion)."""
    monkeypatch.setitem(
        demo_task_fns.STANDARD_FNS,
        "slow",
        lambda: demo_task_fns.SlowSteps(steps=steps, seconds=seconds),
    )


async def test_scenario_arcs(tmp_path: Path) -> None:
    async with _mounted(tmp_path / "data") as (sup, helper):
        # Pipeline: three combined cells complete.
        out = await helper.scenario_pipeline()
        assert set(out["results"]) == {"a", "b", "c"}

        # Staleness fan + resurrection on input b.
        out = await helper.overwrite_input()
        assert out["shout_b"]["status"] == "complete"
        assert out["shout_b"]["currency"] == "stale"
        assert out["combined_b"]["currency"] == "stale"
        out = await helper.revert_input()
        assert out["shout_b"]["currency"] == "fresh"
        assert out["combined_b"]["currency"] == "fresh"

        # Flaky: first demand errors, retry completes.
        out = await helper.scenario_flaky()
        assert out["error"] is not None
        assert out["cell"]["status"] == "errored"
        out = await helper.retry_flaky()
        assert out["cell"]["status"] == "complete"

        # Boundness: unbound demand faults; bind → re-demand completes;
        # release flips it back for the next press.
        out = await helper.scenario_unbound()
        assert out["error"] is not None
        assert out["cell"]["status"] in ("errored", "interrupted")
        await helper.bind_extra()
        out = await helper.rerun_unbound()
        assert out["cell"]["status"] == "complete"
        await helper.release_extra()

        # Subtree workspace: per-variant styled cells complete.
        out = await helper.scenario_subtree()
        assert set(out["results"]) == {"red", "blue"}

        # Run-time task creation: the planned cell completes and its
        # internals hold the grids its run created (a nested task
        # grid included).
        out = await helper.scenario_nested()
        assert out["cell"]["status"] == "complete"
        runner = sup.current
        assert runner is not None
        wf = runner.workflow
        planned = await wf.attach(wf.root, "planned")
        internals = await wf.internals(planned)
        keys = {k.key: k.kind for k in await wf.list_keys(internals)}
        assert keys == {"draft": "data", "polish": "task"}

        # The unsettled chain: echoed reads ONLY shout's output, so an
        # input-b edit leaves it unsettled (observations still match,
        # support stale) — and the revert resurrects freshness.
        out = await helper.scenario_unsettled()
        assert {x: c["currency"] for x, c in out["cells"].items()} == {
            x: "fresh" for x in ("a", "b", "c")
        }
        await helper.overwrite_input()
        echoed = await wf.attach(wf.root, "echoed")
        assert (await wf.inspect(echoed, x="b")).currency == "unsettled"
        assert (await wf.inspect(echoed, x="a")).currency == "fresh"
        await helper.revert_input()
        assert (await wf.inspect(echoed, x="b")).currency == "fresh"


@pytest.mark.xfail(
    run=False,
    reason=(
        "fxtr2 teardown is not cancellation-safe: the crash arc itself"
        " succeeds (interrupted + RunnerCrash, revive + resume OK) but the"
        " process's final teardown hangs on the crashed runner's abandoned"
        " finalizer tasks — see 'Blocked-on followup' in"
        " projects/fxtr_experiment/plans/phase4_monitoring_ui/"
        "chunk3_experiment_demo.md (run=False because the failure mode is a"
        " 30s hang, not a fast red)."
    ),
)
async def test_crash_revive_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The browser-drivable interruption arc: crash the runner mid-slow-
    run (in-process cancellation), the driver survives and reports the
    runner down, revive over the same state, the interrupted cell
    resumes to complete on re-run."""
    _fast_slow(monkeypatch, steps=8, seconds=0.2)
    async with _mounted(tmp_path / "data") as (sup, helper):
        await helper.scenario_pipeline()
        await helper.scenario_slow()

        # Wait until the slow cell is actually running, then crash.
        runner = sup.current
        assert runner is not None
        wf = runner.workflow
        slow = await wf.attach(wf.root, "slow")
        with anyio.fail_after(10):
            while (await wf.inspect(slow)).status != "running":
                await anyio.sleep(0.05)
        out = await helper.crash_runner()
        assert out["crashed"] is True

        # The driver survives; scenario methods fail clearly while down.
        with pytest.raises(Exception, match="down"):
            await helper.scenario_pipeline()

        out = await helper.revive_runner()
        assert out["revived"] is True
        runner = sup.current
        assert runner is not None
        wf = runner.workflow
        slow = await wf.attach(wf.root, "slow")
        report = await wf.inspect(slow)
        assert report.status == "interrupted"

        # Resume: the same generation completes through its checkpoints.
        await helper.rerun_slow()
        with anyio.fail_after(20):
            while (await wf.inspect(slow)).status != "complete":
                await anyio.sleep(0.1)


async def test_relaunch_serves_persisted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """State built in one mount is served by the next (same data dir):
    the content-addressed fn keys line up with the fresh auto-bind, and
    the pipeline is fresh — nothing re-runs (the fresh flaky instance's
    planted failure would fire if its cell re-ran)."""
    _fast_slow(monkeypatch, steps=2, seconds=0.01)
    data = tmp_path / "data"
    async with _mounted(data) as (_sup, helper):
        first = await helper.scenario_pipeline()
        out = await helper.scenario_flaky()
        assert out["cell"]["status"] == "errored"
        out = await helper.retry_flaky()
        assert out["cell"]["status"] == "complete"

    async with _mounted(data) as (_sup, helper):
        again = await helper.scenario_pipeline()
        # Same content-addressed results, served fresh — and the
        # fresh FlakyNote's planted failure did NOT fire, so the
        # flaky cell was served too, not re-run.
        assert again["results"] == first["results"]
        out = await helper.retry_flaky()
        assert out["cell"]["status"] == "complete"
