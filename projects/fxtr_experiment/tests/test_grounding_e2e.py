"""Executor-driven grounding scenarios: real pipelines run through the user
surface, then their grounding verdicts read back at the session level.

Reuses ``test_user_surface.py``'s runner harness (``_local_runner`` yields the
session alongside the ``Workflow``, ``_bind`` wires a task fn) and cribs the
sub-workflow task shape. Unlike ``test_grounding.py`` (hand-planted facts),
these exercise the whole stack — real ``TaskGridConfig`` rules, real
``asserted_grid`` / ``wrote`` / ``read_out`` records — so they cover the paths
the fact-planting suite cannot: ``mint_compatible`` over live rules and the
E↔child mutual recursion of ``notes/grounding.md``.
"""

from pathlib import Path

import pytest
from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment import encoding as e
from fxtr_experiment_task import model as m

from .test_ops_engine import _some_entity
from .test_user_surface import FN_AXES, X_AXES, _bind, _local_runner


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _paths(tmp_path: Path) -> tuple[str, str]:
    return str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")


ZERO = e.encode_coords(())


# -- 8. A real two-task pipeline -------------------------------------------------


class UpTask:
    async def __call__(self, ctx, /, *, inp):
        return await inp.read(x="a")


class DownTask:
    async def __call__(self, ctx, /, *, up):
        return await up.read()


@pytest.mark.anyio
async def test_two_task_pipeline_all_grounded(tmp_path: Path) -> None:
    state, ents = _paths(tmp_path)
    up, down = UpTask(), DownTask()
    async with FxtrScope(name="test-host") as host:
        async with _local_runner(state, ents) as (wf, session):
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "up", up)
            await _bind(wf, host, fns, "down", down)
            up_t = await wf.root.open_task(
                "up", axes=(), task_fn=fns.at(fn="up"), args={"inp": inputs.read_only()}
            )
            down_t = await wf.root.open_task(
                "down", axes=(), task_fn=fns.at(fn="down"), args={"up": up_t.output()}
            )
            # Target the downstream task and demand it: both run.
            await wf.add_target(down_t)
            assert await down_t.output_entity().read() == v1

            # Query the target (root) cell first: one fresh evaluation of the
            # whole justification cone stamps every grid/cell in it grounded,
            # and the shallower queries below then serve those stamps. (Order
            # matters here only because of the stamped-dep re-evaluation bug
            # pinned by tests/test_grounding.py::
            # test_grounded_grid_reeval_with_a_stamped_dep — a correct
            # evaluator is order-independent.)
            rdown = await session.cell_grounding(e.encode_key_path(["down"]), ZERO)
            assert rdown is not None and rdown.verdict == "grounded" and rdown.wanted is True

            # The upstream task cell: grounded + wanted (via the downstream's
            # read_out — demand flows consumer → producer).
            rup = await session.cell_grounding(e.encode_key_path(["up"]), ZERO)
            assert rup is not None and rup.verdict == "grounded" and rup.wanted is True

            # Every grid is grounded.
            for keys in (["fns"], ["inputs"], ["up"], ["down"]):
                r = await session.grid_grounding(e.encode_key_path(keys))
                assert r is not None and r.verdict == "grounded", (keys, r)

            # The user-written input data cell is grounded (user-sourced).
            rin = await session.cell_grounding(
                e.encode_key_path(["inputs"]), e.encode_coords(("a",))
            )
            assert rin is not None and rin.verdict == "grounded"


# -- 9. Mint-compat break (a real rule against a recreated, reshaped grid) -------


@pytest.mark.anyio
async def test_mint_incompat_after_reshaped_recreate(tmp_path: Path) -> None:
    state, ents = _paths(tmp_path)
    up, down = UpTask(), DownTask()
    async with FxtrScope(name="test-host") as host:
        async with _local_runner(state, ents) as (wf, session):
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "up", up)
            await _bind(wf, host, fns, "down", down)
            up_t = await wf.root.open_task(
                "up", axes=(), task_fn=fns.at(fn="up"), args={"inp": inputs.read_only()}
            )
            down_t = await wf.root.open_task(
                "down", axes=(), task_fn=fns.at(fn="down"), args={"up": up_t.output()}
            )
            await wf.add_target(down_t)
            assert await down_t.output_entity().read() == v1

            # Delete the input grid the "up" rule references, then recreate it
            # at the same key with DIFFERENT axes (a different config). The
            # "up" rule's input slice can no longer mint against it. (No
            # grounding query precedes this, so "up" is evaluated fresh below —
            # exercising _mint_incompat's real ViewAxisError path against a
            # live TaskGridConfig rule, the path the smoke never touched.)
            await wf.delete(wf.root, "inputs")
            await wf.root.open_data("inputs", ())  # zero axes now — reshaped

            r = await session.grid_grounding(e.encode_key_path(["up"]))
            assert r is not None and r.verdict == "unanchored"
            assert r.reason is not None and "cannot mint against" in r.reason, r.reason


# -- 10. The forcing argument: E ↔ child mutual recursion ------------------------


class ParentTask:
    """Opens a child task grid inside its subtree, writes an args cell the
    child reads, and reads the child's output (the E of the design note)."""

    async def __call__(self, ctx, /, *, sub, fns, seed):
        v = await seed.read(x="a")
        args = await sub.open_data("args", (m.AxisSpec("k", "str"),))
        await args.write(v, k="in")
        child = await sub.open_task(
            "child", axes=(), task_fn=fns.at(fn="child"), args={"inp": args.read_only()}
        )
        return await child.output_entity().read()


class ChildTask:
    async def __call__(self, ctx, /, *, inp):
        return await inp.read(k="in")


@pytest.mark.anyio
async def test_forcing_argument_parent_child(tmp_path: Path) -> None:
    state, ents = _paths(tmp_path)
    parent, child = ParentTask(), ChildTask()
    async with FxtrScope(name="test-host") as host:
        async with _local_runner(state, ents) as (wf, session):
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            v1 = _some_entity().store().entity_id
            await inputs.write(v1, x="a")
            await _bind(wf, host, fns, "parent", parent)
            await _bind(wf, host, fns, "child", child)
            sub = await wf.root.open_subtree("sub", axes=())
            t = await wf.root.open_task(
                "parent_t",
                axes=(),
                task_fn=fns.at(fn="parent"),
                args={"sub": sub, "fns": fns.read_only(), "seed": inputs.read_only()},
            )
            # Target the parent and run: the whole inner pipeline materializes.
            await wf.add_target(t)
            assert await t.output_entity().read() == v1

            # Each verdict, asserted individually (the derivation must enter
            # the E↔child cycle via would_start(parent), not stall on it).

            # The parent task cell: grounded + wanted (via its target).
            rp = await session.cell_grounding(e.encode_key_path(["parent_t"]), ZERO)
            assert rp is not None and rp.verdict == "grounded" and rp.wanted is True

            # The child task cell: grounded + wanted (wanted via the parent's
            # read of its output — the demand edge that counts from a
            # would-start generation).
            rc = await session.cell_grounding(e.encode_key_path(["sub", "child"]), ZERO)
            assert rc is not None and rc.verdict == "grounded" and rc.wanted is True

            # The args data cell: grounded via the parent's write claim.
            ra = await session.cell_grounding(
                e.encode_key_path(["sub", "args"]), e.encode_coords(("in",))
            )
            assert ra is not None and ra.verdict == "grounded"

            # Every inner grid is grounded (the subtree by user sourcing, the
            # task-created args/child grids by the parent's assertions).
            for keys in (["sub"], ["sub", "args"], ["sub", "child"]):
                r = await session.grid_grounding(e.encode_key_path(keys))
                assert r is not None and r.verdict == "grounded", (keys, r)
