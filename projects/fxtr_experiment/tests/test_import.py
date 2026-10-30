"""The at-rest import (phase-3 chunk 4): destructive load of an exported
layout — import-into-empty round-trip (values serve with no re-runs, the
flip-time probe and lint hold, re-export is byte-identical), the
empty-or-force contract with events surviving the wipe, the refusal
matrix (marker, version, malformed/unsorted rows, checked-redundancy
disagreement, missing entities, running), and the derived rebuilds.
"""

import sqlite3
from pathlib import Path

import pytest

from fxtr2.data.entity_ref_scope import FxtrScope

from fxtr_experiment import encoding as enc
from fxtr_experiment_task.errors import (
    ImportFormatError,
    MissingEntitiesError,
    NotAtRestError,
    StateNotEmptyError,
)
from fxtr_experiment.export import MARKER_NAME
from fxtr_experiment.importer import read_layout, verify_and_decode
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.user_service import open_local
from fxtr_experiment_task.views import DataView

from .test_export import _pipeline_with_task_grid, _tree
from .test_flip_time import assert_record_invariant
from .test_generation_record import _seed_rich_generation
from .test_user_surface import _paths


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_roundtrip_into_empty(tmp_path: Path) -> None:
    """Export → import into a fresh state db over the same store: the
    counts match, results serve with NO re-runs (fresh re-derives from
    re-interned observations), lint is clean, the flip-time invariant
    holds, and re-export is byte-identical."""
    state_a, ents = _paths(tmp_path)
    state_b = str(tmp_path / "state_b.sqlite")
    export_dir = tmp_path / "export"
    async with FxtrScope(name="test-host") as host:
        async with open_local(state_a, ents) as wf:
            await _pipeline_with_task_grid(wf, host)
            export_report = await wf.export_state(str(export_dir))

        async with open_local(state_b, ents) as wf2:
            report = await wf2.import_state(str(export_dir))
            assert (report.grids, report.cells, report.generations) == (5, 3, 1)
            assert (report.sourcing_records, report.targets) == (6, 1)
            assert (
                report.grids,
                report.cells,
                report.generations,
                report.sourcing_records,
                report.targets,
            ) == (
                export_report.grids,
                export_report.cells,
                export_report.generations,
                export_report.sourcing_records,
                export_report.targets,
            )
            assert (await wf2.lint()).clean

            # Served from imported state — no re-run, no re-bind needed
            # for a fresh read (serving consults no connector).
            t = await wf2.attach(wf2.root, "t")
            report_t = await wf2.inspect(t)  # type: ignore[arg-type]
            assert report_t.status == "complete" and report_t.currency == "fresh"
            td = await wf2.attach(await wf2.attach(wf2.root, "sub"), "task_data")  # type: ignore[arg-type]
            assert isinstance(td, DataView)
            assert await td.read(k="cell") is not None

            # Same bytes back out.
            reexport_dir = tmp_path / "reexport"
            await wf2.export_state(str(reexport_dir))
            assert _tree(reexport_dir) == _tree(export_dir)

        async with WorkflowSession.open(state_b, ents) as session:
            await assert_record_invariant(session)
            # grid_refs rebuilt from the decoded rules.
            referrers = await session.grids_referencing(enc.encode_key_path(["inputs"]))
            assert enc.encode_key_path(["t"]) in referrers


@pytest.mark.anyio
async def test_empty_or_force_and_events_survive(tmp_path: Path) -> None:
    """A non-empty state db refuses without ``force``; the force wipe
    replaces state but the events log survives (plus the import event)."""
    state_a, ents = _paths(tmp_path)
    export_dir = tmp_path / "export"
    async with FxtrScope(name="test-host") as host:
        async with open_local(state_a, ents) as wf:
            await _pipeline_with_task_grid(wf, host)
            await wf.export_state(str(export_dir))
            with pytest.raises(StateNotEmptyError, match="force"):
                await wf.import_state(str(export_dir))
            events_before = _event_count(state_a)
            report = await wf.import_state(str(export_dir), force=True)
            assert report.grids == 5
            assert (await wf.lint()).clean
            assert _event_count(state_a) == events_before + 1  # only the import appended


def _event_count(state_db: str) -> int:
    with sqlite3.connect(state_db) as conn:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


@pytest.mark.anyio
async def test_refusal_matrix(tmp_path: Path) -> None:
    """Marker present, version mismatch, malformed and unsorted rows, and
    checked-redundancy disagreement all refuse with named locations —
    before anything is written."""
    state_a, ents = _paths(tmp_path)
    state_b = str(tmp_path / "state_b.sqlite")
    export_dir = tmp_path / "export"
    async with FxtrScope(name="test-host") as host:
        async with open_local(state_a, ents) as wf:
            await _pipeline_with_task_grid(wf, host)
            await wf.export_state(str(export_dir))

        async with open_local(state_b, ents) as wf2:
            marker = export_dir / MARKER_NAME
            marker.write_text("torn")
            with pytest.raises(ImportFormatError, match="in progress or"):
                await wf2.import_state(str(export_dir))
            marker.unlink()

            manifest = export_dir / "manifest.toml"
            original = manifest.read_text()
            manifest.write_text("format_version = 999\n")
            with pytest.raises(ImportFormatError, match="format_version"):
                await wf2.import_state(str(export_dir))
            manifest.write_text(original)

            gens = export_dir / "t" / "generations.tsv"
            good = gens.read_text()
            gens.write_text(good.replace("\tcomplete\t", "\terrored\t"))
            with pytest.raises(ImportFormatError, match="disagrees with the record"):
                await wf2.import_state(str(export_dir))
            gens.write_text("only\ttwo\n")
            with pytest.raises(ImportFormatError, match="columns"):
                await wf2.import_state(str(export_dir))
            gens.write_text(good)

            cells = export_dir / "inputs" / "cells.tsv"
            row = cells.read_text()
            cells.write_text(row + "a-later-row\tsorts-before-nothing\n" + row)
            with pytest.raises(ImportFormatError, match="sorted"):
                await wf2.import_state(str(export_dir))
            cells.write_text("")
            with pytest.raises(ImportFormatError, match="omit-if-empty"):
                await wf2.import_state(str(export_dir))
            cells.write_text(row)

            assert await wf2.import_state(str(export_dir)) is not None  # intact again


@pytest.mark.anyio
async def test_missing_entities_named(tmp_path: Path) -> None:
    """Importing against the wrong entity store lists every missing id
    and writes nothing."""
    state_a, ents_a = _paths(tmp_path)
    export_dir = tmp_path / "export"
    async with FxtrScope(name="test-host") as host:
        async with open_local(state_a, ents_a) as wf:
            await _pipeline_with_task_grid(wf, host)
            await wf.export_state(str(export_dir))

        state_b = str(tmp_path / "state_b.sqlite")
        ents_b = str(tmp_path / "ents_b.sqlite")  # the WRONG store
        async with open_local(state_b, ents_b) as wf2:
            with pytest.raises(MissingEntitiesError, match="missing from the entity store"):
                await wf2.import_state(str(export_dir))
            assert await wf2.list_keys(wf2.root) == ()  # nothing written


@pytest.mark.anyio
async def test_import_refuses_while_running(tmp_path: Path) -> None:
    """The destructive transaction refuses while a generation runs, force
    or not (white-box: a seeded running generation)."""
    state_a, ents = _paths(tmp_path)
    state_b = str(tmp_path / "state_b.sqlite")
    export_dir = tmp_path / "export"
    async with FxtrScope(name="test-host") as host:
        async with open_local(state_a, ents) as wf:
            await _pipeline_with_task_grid(wf, host)
            await wf.export_state(str(export_dir))

    async with WorkflowSession.open(state_b, ents) as session:
        await _seed_rich_generation(session)  # left running
        layout = read_layout(export_dir)
        bundle = await verify_and_decode(session, layout)
        with pytest.raises(NotAtRestError, match="running"):
            await session.apply_import(bundle, True)
