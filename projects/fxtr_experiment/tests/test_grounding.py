"""Grounding verdict scenarios at the session level (the LFP of
``notes/grounding.md``, cached as stamps).

Mirrors ``test_state_store.py``'s harness: a session fixture plus direct
session ops (``create_grid`` / ``upsert_user_sourcing`` / ``insert_generation``
/ ``intern_version`` + ``record_observation`` / ``record_grid_assertion`` /
``add_target`` / ``write_cell``) that plant exactly the extensional facts the
datalog reads, then ``grid_grounding`` / ``cell_grounding`` to read verdicts
back. The extensional facts are hand-built (no executor), so each scenario is
a precise statement about one region of the fixed point.
"""

from pathlib import Path
from typing import AsyncIterator

import pytest
from frozendict import frozendict

from fxtr_experiment import encoding as e
from fxtr_experiment_task import model as m
from fxtr_experiment.ops_engine import new_generation_id
from fxtr_experiment.state_store import WorkflowSession
from fxtr_experiment.storage import (
    OBS_KIND_CELL_VALUE,
    OBS_KIND_WROTE_CELL,
    REQ_KIND_DATA,
    REQ_KIND_TASK_OUTPUT,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[WorkflowSession]:
    async with WorkflowSession.open(
        str(tmp_path / "state.sqlite"), str(tmp_path / "entities.sqlite")
    ) as s:
        yield s


# Two distinct configs → two distinct fingerprints (axis names/counts are
# identity-bearing, so these never collide).
DATA_CFG = m.DataGridConfig(axes=(m.AxisSpec("x", "str"),))
CFG_OTHER = m.DataGridConfig(axes=())
HANDLE_CFG = m.HandleGridConfig(axes=(m.AxisSpec("fn", "str"),))
ZERO = e.encode_coords(())  # the zero-axis cell coords text ('[]')


def _task_cfg() -> m.TaskGridConfig:
    """A minimal task config (an empty-input rule addressing a ``fns`` handle).

    Its own ``rule`` references only ``["fns"]``; the ``grid_dep`` edges a
    scenario wants are supplied explicitly through ``create_grid``'s
    ``rule_refs`` (which the loader reads straight from ``grid_refs``), so the
    rule here never has to line up with them — for any dep that is not the
    rule's ``task_fn`` grid, ``mint_compatible`` is vacuously true and the
    dep's own groundedness governs.
    """
    return m.TaskGridConfig(
        axes=(),
        rule=m.TaskRule(
            task_fn=m.SliceTemplate(
                path=(m.SubgridStepTemplate(cell=(), key="fns"),),
                target_axes=(m.LiteralCoord("f"),),
            ),
            inputs=frozendict(),
        ),
        outputs=m.DEFAULT_TASK_OUTPUTS,
    )


def _val(tag: str) -> e.SliceTemplateEntity:
    """A distinct content-addressed entity per tag (a stand-in cell value)."""
    return e.SliceTemplateEntity(template=m.SliceTemplate(path=(), target_axes=(), output=tag))


async def _grid(
    s: WorkflowSession,
    keys: list[str],
    kind: str,
    config: m.GridConfig,
    *,
    rule_refs: tuple[str, ...] = (),
    source: bool = True,
    source_fp: m.GridConfig | None = None,
) -> str:
    """Create a grid; optionally add its grid-level sourcing record.

    ``source_fp`` (when given) sources with a *different* config's
    fingerprint — a deliberately dangling pin, used to show a mismatched
    sourcing record does not count as a claim.
    """
    path = e.encode_key_path(keys)
    await s.create_grid(path, kind, e.GridConfigEntity(config=config), rule_refs=rule_refs)
    if source:
        await s.upsert_user_sourcing(path, "", e.store_config(source_fp or config))
    return path


async def _gen(
    s: WorkflowSession, keys: list[str], coords: str, *, status: str = "complete"
) -> int:
    """Insert a generation for a task cell (running by default → set status)."""
    fn = e.store_config(CFG_OTHER)
    exec_id = await s.insert_generation(
        new_generation_id(), e.encode_key_path(keys), coords, fn, fn
    )
    if status != "running":
        await s.set_status(exec_id, status)
    return exec_id


async def _read_out(
    s: WorkflowSession,
    reader: int,
    subject_keys: list[str],
    subject_coords: str,
    output: str = "result",
) -> None:
    """Record a resolved ``read_out``: the request edge (the commitment —
    the demand fact grounding reads) plus the resolved observation, as a
    real miss-then-resolve read leaves them."""
    path = e.encode_key_path(subject_keys)
    await s.record_request_edge(reader, path, subject_coords, REQ_KIND_TASK_OUTPUT)
    v = await s.intern_version(path, subject_coords, output, e.store_config(CFG_OTHER))
    await s.record_observation(reader, v, OBS_KIND_CELL_VALUE)


async def _read_data(
    s: WorkflowSession, reader: int, subject_keys: list[str], subject_coords: str
) -> None:
    """Record a resolved ``read_data``: request edge + observation (as
    for `_read_out`; output '')."""
    path = e.encode_key_path(subject_keys)
    await s.record_request_edge(reader, path, subject_coords, REQ_KIND_DATA)
    v = await s.intern_version(path, subject_coords, "", e.store_config(CFG_OTHER))
    await s.record_observation(reader, v, OBS_KIND_CELL_VALUE)


async def _wrote(
    s: WorkflowSession, writer: int, keys: list[str], coords: str, value_id: object
) -> None:
    """Record a ``wrote_cell`` claim at the cell's current value."""
    v = await s.intern_version(e.encode_key_path(keys), coords, "", value_id)  # type: ignore[arg-type]
    await s.record_observation(writer, v, OBS_KIND_WROTE_CELL)


def _slice_for(keys: list[str]) -> object:
    """A distinct stored slice per grid path (targets are PK'd on
    ``(slice_id, expected_config)``, so identical-config grids need
    path-distinct slices)."""
    steps = tuple(m.SubgridStepTemplate(cell=(), key=k) for k in keys)
    return e.store_slice(m.SliceTemplate(path=steps, target_axes=()))


async def _target(
    s: WorkflowSession, keys: list[str], coords: str, config: m.GridConfig
) -> tuple[object, object]:
    """Add an attached target on a cell (its grid must already be sourced)."""
    slice_id = _slice_for(keys)
    cfg = e.store_config(config)
    await s.add_target(slice_id, cfg, e.encode_key_path(keys), coords)  # type: ignore[arg-type]
    return slice_id, cfg


# ---------------------------------------------------------------------------
# 1. The tri-state on grids
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_grid_tristate(session: WorkflowSession) -> None:
    s = session
    # Unclaimed → orphaned.
    orphan = await _grid(s, ["orphan"], "data", DATA_CFG, source=False)
    r = await s.grid_grounding(orphan)
    assert r is not None and r.verdict == "orphaned"
    assert r.reason is not None and "no sourcing record" in r.reason

    # User-sourced (at the current config) → grounded.
    ok = await _grid(s, ["ok"], "data", DATA_CFG)
    r = await s.grid_grounding(ok)
    assert r is not None and r.verdict == "grounded" and r.reason is None

    # Sourced but a rule reference is missing → unanchored naming the ref.
    missing = e.encode_key_path(["gone"])
    broken = await _grid(s, ["broken"], "task", _task_cfg(), rule_refs=(missing,))
    r = await s.grid_grounding(broken)
    assert r is not None and r.verdict == "unanchored"
    assert r.reason is not None and missing in r.reason

    # A grid record sourced with the WRONG fingerprint is a dangling record —
    # it does not claim the grid, so the grid is still orphaned.
    dangling = await _grid(s, ["dangling"], "data", DATA_CFG, source_fp=CFG_OTHER)
    r = await s.grid_grounding(dangling)
    assert r is not None and r.verdict == "orphaned"


# ---------------------------------------------------------------------------
# 2. The tri-state on data cells
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_data_cell_tristate(session: WorkflowSession) -> None:
    s = session
    g = await _grid(s, ["g"], "data", DATA_CFG)  # grounded grid to host cells

    # Unclaimed cell → orphaned (value present, but no sourcing, no writer).
    c_orphan = e.encode_coords(("orphan",))
    await s.write_cell(g, c_orphan, _val("o"))
    r = await s.cell_grounding(g, c_orphan)
    assert r is not None and r.verdict == "orphaned"
    assert r.reason is not None and "no sourcing record" in r.reason

    # Sourced at the current value → grounded.
    c_ok = e.encode_coords(("ok",))
    vid = await s.write_cell(g, c_ok, _val("k"))
    await s.upsert_user_sourcing(g, c_ok, vid)
    r = await s.cell_grounding(g, c_ok)
    assert r is not None and r.verdict == "grounded" and r.reason is None

    # Sourced at a STALE value: pin v1, then overwrite with v2 → the pin no
    # longer matches, so nothing claims the current value → orphaned.
    c_stale = e.encode_coords(("stale",))
    v1 = await s.write_cell(g, c_stale, _val("s1"))
    await s.upsert_user_sourcing(g, c_stale, v1)
    await s.write_cell(g, c_stale, _val("s2"))  # different value now lives here
    r = await s.cell_grounding(g, c_stale)
    assert r is not None and r.verdict == "orphaned"

    # A claimed cell in an UNGROUNDED grid → unanchored ("grid ... not
    # grounded"). Claim it via a writer generation (grid-before-cell forbids
    # user-sourcing a cell in an unsourced grid).
    h = await _grid(s, ["h"], "data", DATA_CFG, source=False)  # orphaned grid
    await _grid(s, ["w"], "task", _task_cfg(), source=False)
    gw = await _gen(s, ["w"], ZERO)
    c_h = e.encode_coords(("c",))
    hvid = await s.write_cell(h, c_h, _val("h"))
    await _wrote(s, gw, ["h"], c_h, hvid)
    r = await s.cell_grounding(h, c_h)
    assert r is not None and r.verdict == "unanchored"
    assert r.reason is not None and "grid" in r.reason and "not grounded" in r.reason


# ---------------------------------------------------------------------------
# 3. Task-cell wanted
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_task_cell_wanted(session: WorkflowSession) -> None:
    s = session
    t = await _grid(s, ["t"], "task", _task_cfg())  # grounded task grid

    # A target → wanted.
    c_tgt = e.encode_coords(("tgt",))
    await _gen(s, ["t"], c_tgt)
    await _target(s, ["t"], c_tgt, _task_cfg())
    r = await s.cell_grounding(t, c_tgt)
    assert r is not None and r.wanted is True

    # No target, but a read_out from a would-start generation → wanted. The
    # reader (c_tgt's gen) would start (targeted + grid grounded), so demand
    # flows to the cell it reads.
    c_read = e.encode_coords(("read",))
    await _gen(s, ["t"], c_read)
    reader = await s.get_generation(t, c_tgt)
    assert reader is not None
    await _read_out(s, reader[0], ["t"], c_read)
    r = await s.cell_grounding(t, c_read)
    assert r is not None and r.wanted is True

    # A reader whose OWN grid is unsourced cannot would-start → unwanted, with
    # the "no reader would start" flavor.
    c_sub = e.encode_coords(("sub",))
    await _gen(s, ["t"], c_sub)
    await _grid(s, ["r"], "task", _task_cfg(), source=False)  # orphaned reader grid
    gr = await _gen(s, ["r"], ZERO)
    await _read_out(s, gr, ["t"], c_sub)
    r = await s.cell_grounding(t, c_sub)
    assert r is not None and r.wanted is False
    assert r.wanted_reason is not None and "no reader would start" in r.wanted_reason

    # A DANGLING target (grid deleted then recreated at a mismatched config)
    # does not count → unwanted.
    d = await _grid(s, ["d"], "task", _task_cfg())
    c_d = e.encode_coords(("cell",))
    gd = await _gen(s, ["d"], c_d)
    await _target(s, ["d"], c_d, _task_cfg())
    await s.delete_generation(gd)
    await s.delete_grid(d)
    # Recreate at the SAME key with a different config → the target's pinned
    # expected_config no longer matches the grid's current config.
    d2 = await _grid(s, ["d"], "task", _task_cfg_other())
    await _gen(s, ["d"], c_d)
    r = await s.cell_grounding(d2, c_d)
    assert r is not None and r.wanted is False
    assert r.wanted_reason is not None and "no attached target" in r.wanted_reason


def _task_cfg_other() -> m.TaskGridConfig:
    """A task config distinct from :func:`_task_cfg` (extra own axis → a
    different canonical fingerprint)."""
    base = _task_cfg()
    return m.TaskGridConfig(axes=(m.AxisSpec("z", "str"),), rule=base.rule, outputs=base.outputs)


# ---------------------------------------------------------------------------
# 4. Status-blindness (running / interrupted count in the datalog)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_status_blindness(session: WorkflowSession) -> None:
    s = session

    # A RUNNING generation's read_out still makes its subject wanted.
    tr = await _grid(s, ["tr"], "task", _task_cfg())
    c_run = e.encode_coords(("runner",))
    grun = await _gen(s, ["tr"], c_run, status="running")
    await _target(s, ["tr"], c_run, _task_cfg())  # runner is wanted + grid grounded
    c_seen = e.encode_coords(("seen",))
    await _gen(s, ["tr"], c_seen)
    await _read_out(s, grun, ["tr"], c_seen)
    r = await s.cell_grounding(tr, c_seen)
    assert r is not None and r.wanted is True

    # The running generation's OWN grounding stamp stays NULL after a query
    # (it derives live — I-G3).
    r = await s.cell_grounding(tr, c_run)
    assert r is not None  # derived live
    row = s._conn.execute(
        "SELECT grounding_verdict FROM task_execs WHERE exec_id = ?", (grun,)
    ).fetchone()
    assert row is not None and row[0] is None
    # ... while its wanted memo IS written (wanted is location-keyed).
    wrow = s._conn.execute(
        "SELECT verdict FROM wanted_stamps WHERE grid_path = ? AND coords = ?", (tr, c_run)
    ).fetchone()
    assert wrow is not None and wrow[0] == "wanted"

    # An INTERRUPTED writer's wrote_cell claim still grounds its written cell.
    tw = await _grid(s, ["tw"], "task", _task_cfg())
    c_w = e.encode_coords(("writer",))
    gw = await _gen(s, ["tw"], c_w, status="interrupted")
    await _target(s, ["tw"], c_w, _task_cfg())  # writer is wanted + grid grounded
    gd = await _grid(s, ["gd"], "data", DATA_CFG)  # grounded data grid
    c_written = e.encode_coords(("written",))
    wvid = await s.write_cell(gd, c_written, _val("w"))
    await _wrote(s, gw, ["gd"], c_written, wvid)
    r = await s.cell_grounding(gd, c_written)
    assert r is not None and r.verdict == "grounded"


# ---------------------------------------------------------------------------
# 5. Would-start chains (upstream-of-upstream demand + groundedness)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_would_start_chain(session: WorkflowSession) -> None:
    s = session
    g1 = await _grid(s, ["g1"], "task", _task_cfg())
    g2 = await _grid(s, ["g2"], "task", _task_cfg())
    g3 = await _grid(s, ["g3"], "task", _task_cfg())
    e1 = await _gen(s, ["g1"], ZERO)
    e2 = await _gen(s, ["g2"], ZERO)
    e3 = await _gen(s, ["g3"], ZERO)
    # T3 reads T2's output; T2 reads T1's output.
    await _read_out(s, e3, ["g2"], ZERO)
    await _read_out(s, e2, ["g1"], ZERO)
    # T3 is the target.
    slice_id, cfg = await _target(s, ["g3"], ZERO, _task_cfg())

    for g in (g1, g2, g3):
        r = await s.cell_grounding(g, ZERO)
        assert r is not None and r.wanted is True, g
        assert r.verdict == "grounded", (g, r.verdict, r.reason)

    # Remove T3's target → the whole chain flips (stamps cleared
    # transitively): all three unwanted, none grounded.
    await s.remove_target(slice_id, cfg)  # type: ignore[arg-type]
    r3 = await s.cell_grounding(g3, ZERO)
    assert r3 is not None and r3.wanted is False and r3.verdict == "orphaned"
    for g in (g1, g2):
        r = await s.cell_grounding(g, ZERO)
        assert r is not None and r.wanted is False, g
        assert r.verdict == "unanchored", (g, r.verdict)


# ---------------------------------------------------------------------------
# 6. Cycles (demand + grid_dep) — the LFP parks them, no special case
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_demand_cycle_needs_a_root(session: WorkflowSession) -> None:
    s = session
    a = await _grid(s, ["a"], "task", _task_cfg())
    b = await _grid(s, ["b"], "task", _task_cfg())
    ea = await _gen(s, ["a"], ZERO)
    eb = await _gen(s, ["b"], ZERO)
    await _read_out(s, ea, ["b"], ZERO)  # a reads b
    await _read_out(s, eb, ["a"], ZERO)  # b reads a
    # No target anywhere → the demand cycle cannot self-support.
    ra = await s.cell_grounding(a, ZERO)
    rb = await s.cell_grounding(b, ZERO)
    assert ra is not None and ra.wanted is False
    assert rb is not None and rb.wanted is False

    # A target on one root breaks the symmetry → both wanted.
    await _target(s, ["a"], ZERO, _task_cfg())
    ra = await s.cell_grounding(a, ZERO)
    rb = await s.cell_grounding(b, ZERO)
    assert ra is not None and ra.wanted is True
    assert rb is not None and rb.wanted is True


@pytest.mark.anyio
async def test_grid_ref_cycle_is_unanchored(session: WorkflowSession) -> None:
    s = session
    a_path = e.encode_key_path(["ca"])
    b_path = e.encode_key_path(["cb"])
    # A references B, B references A; both user-sourced.
    await _grid(s, ["ca"], "task", _task_cfg(), rule_refs=(b_path,))
    await _grid(s, ["cb"], "task", _task_cfg(), rule_refs=(a_path,))
    ra = await s.grid_grounding(a_path)
    rb = await s.grid_grounding(b_path)
    assert ra is not None and ra.verdict == "unanchored"
    assert rb is not None and rb.verdict == "unanchored"


# ---------------------------------------------------------------------------
# 7. Stamp lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stamps_written_and_revoke_clears_only_the_cone(session: WorkflowSession) -> None:
    s = session
    # Two independent grounded chains.
    t = await _grid(s, ["st"], "task", _task_cfg())
    u = await _grid(s, ["su"], "task", _task_cfg())
    await _gen(s, ["st"], ZERO)
    await _gen(s, ["su"], ZERO)
    await _target(s, ["st"], ZERO, _task_cfg())
    await _target(s, ["su"], ZERO, _task_cfg())
    assert (await s.cell_grounding(t, ZERO)).verdict == "grounded"  # type: ignore[union-attr]
    assert (await s.cell_grounding(u, ZERO)).verdict == "grounded"  # type: ignore[union-attr]

    # The stamps are physically present (direct SQL — the O(1) memo).
    def _grid_stamp(path: str) -> object:
        row = s._conn.execute(
            "SELECT grounding_verdict FROM grids WHERE key_path = ?", (path,)
        ).fetchone()
        return None if row is None else row[0]

    assert _grid_stamp(t) == "grounded"
    assert _grid_stamp(u) == "grounded"
    assert (
        s._conn.execute(
            "SELECT verdict FROM wanted_stamps WHERE grid_path = ? AND coords = ?", (t, ZERO)
        ).fetchone()[0]
        == "wanted"
    )

    # Revoking t's sourcing (cascade: it has a target) clears exactly t's
    # cone; u's stamps survive untouched.
    assert await s.revoke_user_sourcing(t, "", cascade=True) is True
    assert _grid_stamp(t) is None
    assert _grid_stamp(u) == "grounded"
    assert (
        s._conn.execute(
            "SELECT verdict FROM wanted_stamps WHERE grid_path = ? AND coords = ?", (u, ZERO)
        ).fetchone()[0]
        == "wanted"
    )


@pytest.mark.anyio
async def test_stamps_survive_reopen_without_rederivation(tmp_path: Path) -> None:
    paths = (str(tmp_path / "s.sqlite"), str(tmp_path / "e.sqlite"))
    w_path = e.encode_key_path(["w"])
    async with WorkflowSession.open(*paths) as s:
        await s.create_grid(w_path, "data", e.GridConfigEntity(config=DATA_CFG))
        await s.upsert_user_sourcing(w_path, "", e.store_config(DATA_CFG))
        assert (await s.grid_grounding(w_path)).verdict == "grounded"  # type: ignore[union-attr]
        as_of = s._conn.execute(
            "SELECT grounding_as_of FROM grids WHERE key_path = ?", (w_path,)
        ).fetchone()[0]
    # Reopen: the stamp is served verbatim (no re-derivation), so its as_of is
    # byte-for-byte unchanged — the only observable proxy for the O(1) serve.
    async with WorkflowSession.open(*paths) as s2:
        r = await s2.grid_grounding(w_path)
        assert r is not None and r.verdict == "grounded"
        as_of2 = s2._conn.execute(
            "SELECT grounding_as_of FROM grids WHERE key_path = ?", (w_path,)
        ).fetchone()[0]
        assert as_of2 == as_of


@pytest.mark.anyio
async def test_turnover_keeps_wanted_memo_clears_departing_claims(session: WorkflowSession) -> None:
    s = session
    t = await _grid(s, ["t"], "task", _task_cfg())
    gd = await _grid(s, ["gd"], "data", DATA_CFG)
    gc = await _gen(s, ["t"], ZERO)
    await _target(s, ["t"], ZERO, _task_cfg())
    # The generation writes (and thereby grounds) a data cell.
    c_w = e.encode_coords(("w",))
    wvid = await s.write_cell(gd, c_w, _val("w"))
    await _wrote(s, gc, ["gd"], c_w, wvid)
    assert (await s.cell_grounding(t, ZERO)).wanted is True  # type: ignore[union-attr]
    assert (await s.cell_grounding(gd, c_w)).verdict == "grounded"  # type: ignore[union-attr]

    # Turnover: the departing record's fan is cleared (the written cell's
    # stamp) but the cell's OWN wanted memo is deliberately left in place
    # (wantedness never depends on the cell's own generation).
    await s.delete_generation(gc)
    assert (
        s._conn.execute(
            "SELECT verdict FROM wanted_stamps WHERE grid_path = ? AND coords = ?", (t, ZERO)
        ).fetchone()[0]
        == "wanted"
    )
    dcell_stamp = s._conn.execute(
        "SELECT grounding_verdict FROM cells WHERE grid_path = ? AND coords = ?", (gd, c_w)
    ).fetchone()[0]
    assert dcell_stamp is None  # the departing writer's claim was cleared


@pytest.mark.anyio
async def test_grounded_grid_reeval_with_a_stamped_dep(session: WorkflowSession) -> None:
    s = session
    fns = await _grid(s, ["fns"], "handle", HANDLE_CFG)  # a real mint target
    a = await _grid(s, ["a"], "task", _task_cfg(), rule_refs=(fns,))  # task_fn → fns
    # Stamp the dep with an independent query (routine); it becomes a pruned
    # constant for the next evaluation.
    assert (await s.grid_grounding(fns)).verdict == "grounded"  # type: ignore[union-attr]
    # A is genuinely grounded (sourced; its only dep fns is grounded and its
    # task_fn slice mints against the handle). A fresh evaluation confirms it;
    # this re-evaluation should too.
    r = await s.grid_grounding(a)
    assert r is not None and r.verdict == "grounded"
