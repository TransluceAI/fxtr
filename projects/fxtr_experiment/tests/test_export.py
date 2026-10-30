"""The at-rest export (phase-3 chunk 3): the deterministic layout writer,
the proven-ownership guard, the in-progress marker, the running refusal,
and content correctness against ``notes/at_rest_format.md`` — including
the extraction contract (id-typed columns are the complete root set).
Driven through the public surface (`Workflow.export_state`) with a
white-box facet for the refusal and guard cases.
"""

import hashlib
import tomllib
from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment import encoding as enc
from fxtr_experiment_task import model as m
from fxtr_experiment_task.errors import ExportLayoutError, NotAtRestError
from fxtr_experiment.export import MARKER_NAME, validate_export_dir, write_export
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.user_service import Workflow, open_local

from .test_generation_record import _seed_rich_generation
from .test_lint import TASK_DATA_PATH
from .test_user_surface import FN_AXES, X_AXES, MakesDataGrid, _bind, _paths
from .test_ops_engine import _some_entity


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _tree(root: Path) -> dict[str, bytes]:
    """The export dir as {relative posix path: bytes} (dot-entries skipped)."""
    out = {}
    for p in sorted(root.rglob("*")):
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def _digest(tree: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name, data in sorted(tree.items()):
        h.update(name.encode() + b"\0" + data + b"\0")
    return h.hexdigest()


async def _pipeline_with_task_grid(wf: Workflow, host: FxtrScope) -> None:
    """fns + inputs + the maker task (creates sub/task_data and writes a
    cell) — a tree with every fact kind: handle/data/task grids, a
    task-created grid, sourcing, a target, a generation."""
    fns = await wf.open_handle(wf.root, "fns", FN_AXES)
    inputs = await wf.root.open_data("inputs", X_AXES)
    v1 = _some_entity().store().entity_id
    await inputs.write(v1, x="a")
    await _bind(wf, host, fns, "maker", MakesDataGrid())
    sub = await wf.root.open_subtree("sub", axes=())
    t = await wf.root.open_task(
        "t", axes=(), task_fn=fns.at(fn="maker"), args={"sub": sub, "seed": inputs.read_only()}
    )
    assert await wf.demand(t.output_entity()) == v1


@pytest.mark.anyio
async def test_layout_content_and_determinism(tmp_path: Path) -> None:
    """The exported tree mirrors the grid tree with the spec's files and
    columns; re-export is byte-identical; a one-fact mutation shows up as
    exactly the expected file-level change."""
    state, ents = _paths(tmp_path)
    export_dir = tmp_path / "export"
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            await _pipeline_with_task_grid(wf, host)
            report = await wf.export_state(str(export_dir))
            assert (report.grids, report.cells, report.generations) == (5, 3, 1)
            # 4 user-created grids + the handle-cell and input-cell writes.
            assert report.sourcing_records == 6
            assert report.targets == 1

            tree = _tree(export_dir)
            manifest = tomllib.loads(tree["manifest.toml"].decode())
            assert manifest == {"format_version": 1}
            # The grid tree is the directory tree (task_data nested in sub).
            assert set(tree) == {
                "manifest.toml",
                "fns/grid.toml",
                "fns/cells.tsv",
                "fns/sourcing.tsv",
                "inputs/grid.toml",
                "inputs/cells.tsv",
                "inputs/sourcing.tsv",
                "sub/grid.toml",
                "sub/sourcing.tsv",
                "sub/task_data/grid.toml",
                "sub/task_data/cells.tsv",  # task-created: no sourcing.tsv
                "t/grid.toml",
                "t/generations.tsv",
                "t/sourcing.tsv",
                "t/targets.tsv",
            }
            grid_toml = tomllib.loads(tree["t/grid.toml"].decode())
            assert grid_toml["kind"] == "task" and grid_toml["config_id"]
            row = tree["t/generations.tsv"].decode().splitlines()[0].split("\t")
            assert row[0] == "[]" and row[2] == "complete"  # coords, gen, status, record

            # Deterministic: re-export of the same state is byte-identical.
            await wf.export_state(str(export_dir))
            assert _digest(_tree(export_dir)) == _digest(tree)
            assert not (export_dir / MARKER_NAME).exists()

            # One mutation → one file-level diff (plus nothing else).
            # (_some_entity is content-addressed constant — build a
            # genuinely different value.)
            v2 = (
                enc.GridConfigEntity(config=m.DataGridConfig(axes=(m.AxisSpec("zz", "str"),)))
                .store()
                .entity_id
            )
            inputs = await wf.attach(wf.root, "inputs")
            await wf.overwrite(inputs, v2, x="a")  # type: ignore[arg-type]
            await wf.export_state(str(export_dir))
            tree2 = _tree(export_dir)
            changed = {k for k in tree if tree[k] != tree2[k]}
            assert changed == {"inputs/cells.tsv", "inputs/sourcing.tsv"}


@pytest.mark.anyio
async def test_extraction_contract(tmp_path: Path) -> None:
    """Collecting the id-typed columns of the layout yields exactly the
    referenced roots, each loadable from the shared store (a rich state:
    results, checkpoints, failures included via the seeded generation)."""
    state, ents = _paths(tmp_path)
    export_dir = tmp_path / "export"
    async with WorkflowSession.open(state, ents) as session:
        exec_id, _ = await _seed_rich_generation(session)
        # Give the referenced side grids rows so the FK-less references in
        # the record stay honest facts about the export's own grids.
        await session.set_status(exec_id, "errored")
        snapshot = await session.export_snapshot()
        write_export(snapshot, export_dir)

        ids = set()
        for _path, _kind, config_id in snapshot.grids:
            ids.add(config_id)
        for _g, _c, value_id in snapshot.cells:
            ids.add(value_id)
        for _g, _c, _gen, _status, record_id in snapshot.generations:
            ids.add(record_id)
        for _g, _c, fp in snapshot.sourcing:
            ids.add(fp)
        for _g, _c, slice_id, expected in snapshot.targets:
            ids.update((slice_id, expected))
        for entity_id in ids:
            assert await session.ensure_loadable(entity_id) == entity_id


@pytest.mark.anyio
async def test_refuses_while_running(tmp_path: Path) -> None:
    """The layout is an at-rest format: a running generation refuses the
    snapshot atomically (nothing written)."""
    state, ents = _paths(tmp_path)
    export_dir = tmp_path / "export"
    async with WorkflowSession.open(state, ents) as session:
        await _seed_rich_generation(session)  # inserted running
        with pytest.raises(NotAtRestError, match="running"):
            await session.export_snapshot()
    assert not export_dir.exists()


@pytest.mark.anyio
async def test_ownership_is_proved_not_assumed(tmp_path: Path) -> None:
    """A directory containing anything non-export-shaped refuses wholesale
    with nothing modified — misdirection is a safe error; dot-entries are
    invisible; a previous export (even with strays *added*) refuses too."""
    state, ents = _paths(tmp_path)
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            await _pipeline_with_task_grid(wf, host)

            precious = tmp_path / "precious"
            precious.mkdir()
            (precious / "thesis.txt").write_text("years of work")
            (precious / ".DS_Store").write_bytes(b"noise")  # invisible alone
            with pytest.raises(ExportLayoutError, match="thesis.txt"):
                await wf.export_state(str(precious))
            assert (precious / "thesis.txt").read_text() == "years of work"

            export_dir = tmp_path / "export"
            await wf.export_state(str(export_dir))
            (export_dir / ".DS_Store").write_bytes(b"noise")
            await wf.export_state(str(export_dir))  # dot-entries never block
            (export_dir / "notes.md").write_text("stray")
            with pytest.raises(ExportLayoutError, match="notes.md"):
                await wf.export_state(str(export_dir))
            assert (export_dir / "notes.md").read_text() == "stray"
            # A bad name *nested in a grid dir* refuses too.
            (export_dir / "notes.md").unlink()
            (export_dir / "t" / "scratch.py").write_text("stray")
            with pytest.raises(ExportLayoutError, match="scratch.py"):
                await wf.export_state(str(export_dir))


@pytest.mark.anyio
async def test_marker_and_pruning(tmp_path: Path) -> None:
    """A leftover marker (a crashed export) never blocks re-export and is
    removed by a completed one; grids deleted from the state lose their
    directories (stale members pruned)."""
    state, ents = _paths(tmp_path)
    export_dir = tmp_path / "export"
    async with FxtrScope(name="test-host") as host:
        async with open_local(state, ents) as wf:
            await _pipeline_with_task_grid(wf, host)
            await wf.export_state(str(export_dir))

            # Simulate a crashed export: the marker is present, content torn.
            (export_dir / MARKER_NAME).write_text("torn")
            (export_dir / "inputs" / "cells.tsv").unlink()
            await wf.export_state(str(export_dir))
            assert not (export_dir / MARKER_NAME).exists()
            assert (export_dir / "inputs" / "cells.tsv").exists()

            # Deleting the task-created grid prunes its directory (and the
            # generations file follows the cleared cell).
            task_data = enc.decode_key_path(TASK_DATA_PATH)
            assert (export_dir / Path(*task_data)).is_dir()
            t = await wf.attach(wf.root, "t")
            await wf.clear(t)  # type: ignore[arg-type]
            sub = await wf.attach(wf.root, "sub")
            await wf.delete(sub, "task_data")  # type: ignore[arg-type]
            await wf.export_state(str(export_dir))
            assert not (export_dir / Path(*task_data)).exists()
            assert not (export_dir / "t" / "generations.tsv").exists()
            validate_export_dir(export_dir)  # still a clean layout
