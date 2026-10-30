"""The phase-3 cross-cutting suites (chunk 5 + its stress follow-up):

- the **messy-state round-trip** (any at-rest state serializes —
  errored, interrupted, ungrounded, dangling, checkpoints and internals
  included — and import reproduces it verdict-for-verdict and
  byte-for-byte);
- the **real-git e2e** over ``data → task1 → task2``
  (partial-then-completed history; both restore flavors: result
  fidelity — a completed state serves with zero re-runs — and
  definition fidelity — a partial state re-runs the rest to
  content-addressed-equal values);
- **divergent versions**: same paths, *different* bytes across commits
  (expected-different asserted different, expected-same asserted
  byte-identical — staleness provably derived, not stored), with a
  second workflow database swapping between versions it never natively
  held (values can only have come from git), stale-at-rest verdicts
  round-tripping, and clear+re-run converging across versions;
- **cross-database collaboration**: B imports A's partial commit,
  extends it, commits; A re-imports the extension, serves it with zero
  re-runs, and keeps working on it;
- **resume-after-import**: a generation interrupted after a completed
  step, imported into a fresh db, resumed — the step replays from the
  imported checkpoint row, executing exactly once across
  fault → export → import → resume → completion.
"""

import subprocess
import uuid as _uuid
from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope
from fxtr2.data.system_entities import define_type

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import CellFaultError
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.user_service import open_local
from fxtr_experiment_task.view_runtime import step_fn

from .test_export import _tree
from .test_flip_time import assert_record_invariant
from .test_lint import K_AXES, ReadsCellOnly
from .test_ops_engine import _some_entity
from .test_user_surface import FN_AXES, X_AXES, _bind, _paths


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _distinct_entity(tag: str):
    """A content-addressed entity distinct per ``tag``."""
    return enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec(tag, "str"),)))


# -- 1. The messy-state round-trip ------------------------------------------------


@step_fn(type_id=define_type(_uuid.UUID("6c8f3d2b-0d4a-4b19-9c72-3a5f1d0e8b46"), "e2e_noted"))
async def _noted(ctx, *, tag):
    return f"noted-{tag}"


class BuildsInternals:
    """Creates internals (data + checkpoint grids) and runs a step."""

    async def __call__(self, ctx, /, *, inp):
        value = await inp.read(x="a")
        internals = ctx.own_internals()
        scratch = await internals.open_data("scratch", (m.AxisSpec("s", "str"),))
        await scratch.write(value, s="kept")
        ckpt = await internals.open_checkpoint("steps", axes=())
        await ckpt.run_step(_noted, args={"tag": "x"}, coords={})
        return value


class Raises:
    async def __call__(self, ctx, /, *, inp):
        raise RuntimeError("boom")


@pytest.mark.anyio
async def test_messy_state_roundtrip(tmp_path: Path) -> None:
    """Errored (failures intact), interrupted (request edges intact),
    orphaned, dangling sourcing + target, internals with checkpoints —
    the whole mess imports verdict-for-verdict (lint and inspections
    equal) and re-exports byte-for-byte."""
    state_a, ents = _paths(tmp_path)
    state_b = str(tmp_path / "state_b.sqlite")
    export_dir = tmp_path / "export"
    async with FxtrScope(name="test-host") as host:
        async with open_local(state_a, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            await inputs.write(_some_entity().store().entity_id, x="a")
            await _bind(wf, host, fns, "builder", BuildsInternals())
            await _bind(wf, host, fns, "raises", Raises())
            await _bind(wf, host, fns, "reader", ReadsCellOnly())

            # Complete, with internals + a checkpointed step.
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="builder"), args={"inp": inputs.read_only()}
            )
            await wf.demand(t.output_entity())
            # Errored (its own fault, failure records).
            bad = await wf.root.open_task(
                "bad", axes=(), task_fn=fns.at(fn="raises"), args={"inp": inputs.read_only()}
            )
            with pytest.raises(CellFaultError, match="errored"):
                await wf.demand(bad.output_entity())
            # Interrupted (reads a cell nothing wrote — request edge survives).
            d = await wf.root.open_data("d", K_AXES)
            r = await wf.root.open_task(
                "r", axes=(), task_fn=fns.at(fn="reader"), args={"cell": d.read_only()}
            )
            with pytest.raises(CellFaultError, match="interrupted"):
                await wf.demand(r.output_entity())
            # Orphaned: revoke the data grid's sourcing wholesale.
            await d.write(_distinct_entity("z").store().entity_id, k="cell")
            await wf.revoke_grid_sourcing(wf.root, "d", force=True)
            # Dangling: config-drifted target + sourcing.
            aux = await wf.root.open_data("aux", K_AXES)
            await aux.write(_distinct_entity("w").store().entity_id, k="z")
            await wf.add_target(aux, k="z")
            await wf.delete_grid(wf.root, "aux")
            await wf.root.open_data("aux", (m.AxisSpec("j", "int"),))

            lint_a = await wf.lint()
            assert not lint_a.sound and lint_a.ungrounded  # genuinely messy
            inspect_bad = await wf.inspect(bad)
            inspect_r = await wf.inspect(r)
            assert inspect_bad.status == "errored" and inspect_r.status == "interrupted"
            await wf.export_state(str(export_dir))

        async with open_local(state_b, ents) as wf2:
            await wf2.import_state(str(export_dir))
            lint_b = await wf2.lint()
            assert lint_b == lint_a  # verdict-for-verdict, diagnoses included
            bad2 = await wf2.attach(wf2.root, "bad")
            r2 = await wf2.attach(wf2.root, "r")
            assert await wf2.inspect(bad2) == inspect_bad  # type: ignore[arg-type]
            assert await wf2.inspect(r2) == inspect_r  # type: ignore[arg-type]
            reexport = tmp_path / "reexport"
            await wf2.export_state(str(reexport))
            assert _tree(reexport) == _tree(export_dir)

        async with WorkflowSession.open(state_b, ents) as session:
            await assert_record_invariant(session)


# -- 2. The real-git e2e -----------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "user.name=e2e", "-c", "user.email=e2e@test", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


class EchoScalar:
    """Per-cell echo (its input's x axis is vmap-consumed)."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx, /, *, inp):
        self.calls += 1
        return await inp.read()


class Combines:
    """Reads both pinned task1 outputs; returns the first."""

    async def __call__(self, ctx, /, *, a, b):
        va = await a.read()
        await b.read()
        return va


@pytest.mark.anyio
async def test_real_git_history_and_both_restores(tmp_path: Path) -> None:
    """The collaboration arc: commit A (task1 partially run), commit B
    (task2 + the rest of task1 — cached cells served, not re-run),
    then restore-to-partial (force-import A; re-run reproduces B's
    values; task1's imported cell never re-runs) and restore-to-completed
    (force-import B; everything serves, zero re-runs, lint clean)."""
    state, ents = _paths(tmp_path)
    repo = tmp_path / "repo"
    echo = EchoScalar()
    v = {"a": _distinct_entity("a").store().entity_id, "b": _distinct_entity("b").store().entity_id}
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            await inputs.write(v["a"], x="a")
            await inputs.write(v["b"], x="b")
            await _bind(wf, host, fns, "echo", echo)
            await _bind(wf, host, fns, "combine", Combines())
            t1 = await wf.root.open_task(
                "t1",
                axes=(m.AxisSpec("x", "str"),),
                task_fn=fns.at(fn="echo"),
                args={"inp": inputs.read_only()},
            )

            # Commit A: a PARTIAL grid — only t1[a] has run.
            assert await wf.demand(t1.output_entity(), x="a") == v["a"]
            assert echo.calls == 2  # main + safety replay
            await wf.export_state(str(repo))
            _git(repo, "init", "-q")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "A: partial task1")
            sha_a = _git(repo, "rev-parse", "HEAD")
            tree_a = _tree(repo)

            # The larger run: task2 over both cells — t1[a] SERVES (no
            # re-run), t1[b] executes, t2 computes.
            t2 = await wf.root.open_task(
                "t2",
                axes=(),
                task_fn=fns.at(fn="combine"),
                args={"a": t1.output_entity().at(x="a"), "b": t1.output_entity().at(x="b")},
            )
            assert await wf.demand(t2.output_entity()) == v["a"]
            assert echo.calls == 4  # only t1[b]'s run (+ its safety replay)
            await wf.export_state(str(repo))
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "B: task2 over the full grid")
            sha_b = _git(repo, "rev-parse", "HEAD")
            tree_b = _tree(repo)
            # The A→B diff is row/file-level: t1 gained a generation row
            # and a target row; t2 appeared; nothing else moved.
            changed = {k for k in tree_a if k in tree_b and tree_a[k] != tree_b[k]}
            added = set(tree_b) - set(tree_a)
            assert changed == {"t1/generations.tsv"}
            assert added == {
                "t2/grid.toml",
                "t2/generations.tsv",
                "t2/targets.tsv",
                "t2/sourcing.tsv",
            }

            # Restore-to-partial (the hard-reset flow): back to A, re-run.
            _git(repo, "reset", "-q", "--hard", sha_a)
            await wf.import_state(str(repo), force=True)
            assert await wf.demand(t1.output_entity(), x="a") == v["a"]
            assert echo.calls == 4  # the imported cell SERVES — no re-run
            t2 = await wf.root.open_task(  # A has no t2; recreate the same rule
                "t2",
                axes=(),
                task_fn=fns.at(fn="combine"),
                args={"a": t1.output_entity().at(x="a"), "b": t1.output_entity().at(x="b")},
            )
            assert await wf.demand(t2.output_entity()) == v["a"]  # B's value, regenerated
            assert echo.calls == 6  # t1[b] re-ran (it was not in A)
            regen = tmp_path / "regen"
            await wf.export_state(str(regen))
            tree_regen = _tree(regen)
            # Value-level equality with B everywhere; byte-identity for
            # everything except the re-run generations (fresh uuids —
            # never byte-compared across re-runs, by design).
            assert set(tree_regen) == set(tree_b)
            differing = {k for k in tree_b if tree_b[k] != tree_regen[k]}
            assert differing == {"t1/generations.tsv", "t2/generations.tsv"}

            # Restore-to-completed: back to B; everything serves as-is.
            _git(repo, "reset", "-q", "--hard", sha_b)
            await wf.import_state(str(repo), force=True)
            assert await wf.demand(t2.output_entity()) == v["a"]
            assert await wf.demand(t1.output_entity(), x="b") == v["b"]
            assert echo.calls == 6  # zero re-runs: result fidelity
            assert (await wf.lint()).clean

    async with WorkflowSession.open(state, ents) as session:
        await assert_record_invariant(session)


# -- 3. Divergent versions: same paths, different bytes, value flips --------------

_STEP_CALLS = {"n": 0}


@step_fn(type_id=define_type(_uuid.UUID("7d9e4c3a-1e5b-4c2a-8d83-4b6f2e1f9c57"), "e2e_counted"))
async def _counted(ctx, *, tag):
    _STEP_CALLS["n"] += 1
    return f"c-{tag}"


@pytest.mark.anyio
async def test_git_divergent_inputs_and_value_flips_across_databases(tmp_path: Path) -> None:
    """Three commits of ONE cell lineage — V1 (t1[a] from v1a), V1_stale
    (input overwritten, nothing re-run), V2 (cleared + re-run from v2a) —
    with the expected-different files asserted to actually differ (and
    the expected-same to stay byte-identical: the untouched sibling
    row, and V1→V1_stale's generations file — staleness is derived,
    never stored). A SECOND workflow database (fresh state db, same
    store) then swaps between them: it imported V2 first, so its session
    never held V1 — reading v1a after force-importing V1 proves the
    state came from git, not session residue. Stale-at-rest round-trips
    with its verdict, and re-running converges to V2's value."""
    state_a, ents = _paths(tmp_path)
    state_b = str(tmp_path / "state_b.sqlite")
    repo = tmp_path / "repo"
    echo = EchoScalar()
    v1a = _distinct_entity("v1a").store().entity_id
    v2a = _distinct_entity("v2a").store().entity_id
    vb = _distinct_entity("vb").store().entity_id
    junk = _distinct_entity("junk").store().entity_id
    async with FxtrScope(name="test-host") as host:
        async with open_local(state_a, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            inputs = await wf.root.open_data("inputs", X_AXES)
            await inputs.write(v1a, x="a")
            await inputs.write(vb, x="b")
            await _bind(wf, host, fns, "echo", echo)
            t1 = await wf.root.open_task(
                "t1",
                axes=(m.AxisSpec("x", "str"),),
                task_fn=fns.at(fn="echo"),
                args={"inp": inputs.read_only()},
            )
            assert await wf.demand(t1.output_entity(), x="a") == v1a
            assert await wf.demand(t1.output_entity(), x="b") == vb
            await wf.export_state(str(repo))
            _git(repo, "init", "-q")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "V1")
            sha_v1 = _git(repo, "rev-parse", "HEAD")
            tree_v1 = _tree(repo)

            # V1_stale: the input changed; nothing re-ran. Staleness is
            # DERIVED — the generations file must be byte-identical.
            await wf.overwrite(inputs, v2a, x="a")
            report = await wf.inspect(t1, x="a")
            assert report.currency == "stale"
            await wf.export_state(str(repo))
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "V1_stale")
            sha_v1_stale = _git(repo, "rev-parse", "HEAD")
            tree_v1_stale = _tree(repo)
            assert {k for k in tree_v1 if tree_v1[k] != tree_v1_stale[k]} == {
                "inputs/cells.tsv",
                "inputs/sourcing.tsv",  # overwriting re-sources
            }

            # V2: clear + re-run t1[a] — the new value flows through.
            await wf.clear(t1, x="a")
            assert await wf.demand(t1.output_entity(), x="a") == v2a
            await wf.export_state(str(repo))
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "V2")
            sha_v2 = _git(repo, "rev-parse", "HEAD")
            tree_v2 = _tree(repo)
            # Same paths, different bytes — exactly where expected.
            assert {k for k in tree_v1_stale if tree_v1_stale[k] != tree_v2[k]} == {
                "t1/generations.tsv"
            }

            # Row-level: the untouched sibling's row is byte-identical
            # across all three; the re-run cell's row changed.
            def row_of(tree: dict, coord: str) -> str:
                (line,) = [
                    ln
                    for ln in tree["t1/generations.tsv"].decode().splitlines()
                    if ln.startswith(f'["{coord}"]\t')
                ]
                return line

            assert row_of(tree_v1, "b") == row_of(tree_v2, "b")
            assert row_of(tree_v1, "a") != row_of(tree_v2, "a")

        # The second database: V2 first (so V1 was never session state).
        # Import into the EMPTY db first; binding attaches afterwards
        # (the same-value handle write is idempotent).
        async with open_local(state_b, ents) as wf2:
            await wf2.import_state(str(repo))  # at V2
            fns2 = await wf2.open_handle(wf2.root, "fns", FN_AXES)
            await _bind(wf2, host, fns2, "echo", echo)
            t1 = await wf2.attach(wf2.root, "t1")
            inputs2 = await wf2.attach(wf2.root, "inputs")
            calls = echo.calls
            assert await wf2.demand(t1.output_entity(), x="a") == v2a  # type: ignore[attr-defined]
            assert echo.calls == calls  # served, not re-run

            # Junk-mutate, then force-import V1: the junk must be GONE and
            # the OLD value back — loaded from git, not from this session.
            await wf2.overwrite(inputs2, junk, x="a")  # type: ignore[arg-type]
            _git(repo, "reset", "-q", "--hard", sha_v1)
            await wf2.import_state(str(repo), force=True)
            t1 = await wf2.attach(wf2.root, "t1")
            inputs2 = await wf2.attach(wf2.root, "inputs")
            assert await inputs2.read(x="a") == v1a  # type: ignore[attr-defined]
            assert await wf2.demand(t1.output_entity(), x="a") == v1a  # type: ignore[attr-defined]
            assert echo.calls == calls  # V1 is fresh w.r.t. v1a: served

            # Stale-at-rest: the verdict (and its diagnosis) round-trips;
            # the documented user move converges to V2's value.
            _git(repo, "reset", "-q", "--hard", sha_v1_stale)
            await wf2.import_state(str(repo), force=True)
            t1 = await wf2.attach(wf2.root, "t1")
            report = await wf2.inspect(t1, x="a")  # type: ignore[arg-type]
            assert report.currency == "stale" and report.reason is not None
            with pytest.raises(CellFaultError, match="stale"):
                await wf2.demand(t1.output_entity(), x="a")  # type: ignore[attr-defined]
            await wf2.clear(t1, x="a")  # type: ignore[arg-type]
            assert await wf2.demand(t1.output_entity(), x="a") == v2a  # type: ignore[attr-defined]
            assert (await wf2.lint()).clean
            _git(repo, "reset", "-q", "--hard", sha_v2)  # leave the repo tidy


# -- 4. Cross-database collaboration: extend elsewhere, pull back ----------------


@pytest.mark.anyio
async def test_cross_database_extension_roundtrip(tmp_path: Path) -> None:
    """The collaboration loop: A commits partial work; B (its own state
    db) imports, EXTENDS it (the rest of task1 + task2), commits; A
    re-imports B's extension, serves it with zero re-runs — and keeps
    working on it (imported state is live, not read-only)."""
    state_a, ents = _paths(tmp_path)
    state_b = str(tmp_path / "state_b.sqlite")
    repo = tmp_path / "repo"
    echo = EchoScalar()
    va = _distinct_entity("cda").store().entity_id
    vb = _distinct_entity("cdb").store().entity_id
    vb2 = _distinct_entity("cdb2").store().entity_id

    async def _setup(wf, host):
        fns = await wf.open_handle(wf.root, "fns", FN_AXES)
        await _bind(wf, host, fns, "echo", echo)
        await _bind(wf, host, fns, "combine", Combines())
        return fns

    async with FxtrScope(name="test-host") as host:
        async with open_local(state_a, ents) as wf:
            fns = await _setup(wf, host)
            inputs = await wf.root.open_data("inputs", X_AXES)
            await inputs.write(va, x="a")
            await inputs.write(vb, x="b")
            t1 = await wf.root.open_task(
                "t1",
                axes=(m.AxisSpec("x", "str"),),
                task_fn=fns.at(fn="echo"),
                args={"inp": inputs.read_only()},
            )
            assert await wf.demand(t1.output_entity(), x="a") == va
            await wf.export_state(str(repo))
            _git(repo, "init", "-q")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "A: partial")

        async with open_local(state_b, ents) as wfb:
            await wfb.import_state(str(repo))  # into the empty db, THEN bind
            fnsb = await _setup(wfb, host)
            t1 = await wfb.attach(wfb.root, "t1")
            calls = echo.calls
            t2 = await wfb.root.open_task(
                "t2",
                axes=(),
                task_fn=fnsb.at(fn="combine"),
                args={"a": t1.output_entity().at(x="a"), "b": t1.output_entity().at(x="b")},  # type: ignore[attr-defined]
            )
            assert await wfb.demand(t2.output_entity()) == va
            assert echo.calls == calls + 2  # only t1[b] ran here
            await wfb.export_state(str(repo))
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "B: extended")

        async with open_local(state_a, ents) as wf:
            fns = await _setup(wf, host)
            calls = echo.calls
            await wf.import_state(str(repo), force=True)
            t1 = await wf.attach(wf.root, "t1")
            t2 = await wf.attach(wf.root, "t2")
            assert await wf.demand(t2.output_entity()) == va  # type: ignore[attr-defined]
            assert echo.calls == calls  # B's computed extension SERVES in A
            # ...and A can keep working on the imported state.
            inputs = await wf.attach(wf.root, "inputs")
            await wf.overwrite(inputs, vb2, x="b")  # type: ignore[arg-type]
            await wf.clear(t1, x="b")  # type: ignore[arg-type]
            assert await wf.demand(t1.output_entity(), x="b") == vb2  # type: ignore[attr-defined]


# -- 5. Resume-after-import: checkpoint replay across databases -------------------


class StepThenRead:
    """Runs a checkpointed step, then reads a cell that may not exist yet
    (interrupting the first run at the read)."""

    async def __call__(self, ctx, /, *, cell):
        internals = ctx.own_internals()
        ckpt = await internals.open_checkpoint("steps", axes=())
        await ckpt.run_step(_counted, args={"tag": "r"}, coords={})
        return await cell.read(k="k")


@pytest.mark.anyio
async def test_resume_after_import_replays_imported_checkpoint(tmp_path: Path) -> None:
    """The crashed-on-one-machine, resumed-on-another story: a generation
    interrupted AFTER a completed step is exported and imported into a
    fresh db; supplying the missing input and re-demanding resumes it —
    the step replays from the IMPORTED checkpoint row, executing exactly
    once across fault → export → import → resume → completion (safety
    replay included)."""
    _STEP_CALLS["n"] = 0
    state_a, ents = _paths(tmp_path)
    state_b = str(tmp_path / "state_b.sqlite")
    export_dir = tmp_path / "export"
    vk = _distinct_entity("vk").store().entity_id
    async with FxtrScope(name="test-host") as host:
        async with open_local(state_a, ents) as wf:
            fns = await wf.open_handle(wf.root, "fns", FN_AXES)
            await _bind(wf, host, fns, "stepread", StepThenRead())
            d = await wf.root.open_data("d", K_AXES)  # created, cell unwritten
            t = await wf.root.open_task(
                "t", axes=(), task_fn=fns.at(fn="stepread"), args={"cell": d.read_only()}
            )
            with pytest.raises(CellFaultError, match="interrupted"):
                await wf.demand(t.output_entity())
            assert _STEP_CALLS["n"] == 1  # the step DID run before the fault
            await wf.export_state(str(export_dir))

        async with open_local(state_b, ents) as wf2:
            await wf2.import_state(str(export_dir))  # into the empty db, THEN bind
            fns2 = await wf2.open_handle(wf2.root, "fns", FN_AXES)
            await _bind(wf2, host, fns2, "stepread", StepThenRead())
            d2 = await wf2.attach(wf2.root, "d")
            await d2.write(vk, k="k")  # type: ignore[attr-defined]  # supply the missing input
            t2 = await wf2.attach(wf2.root, "t")
            assert await wf2.demand(t2.output_entity()) == vk  # type: ignore[attr-defined]
            # Once, total: the resume served the step from the imported
            # checkpoint, and the safety replay hit it too.
            assert _STEP_CALLS["n"] == 1
            assert (await wf2.lint()).clean

    async with WorkflowSession.open(state_b, ents) as session:
        await assert_record_invariant(session)
