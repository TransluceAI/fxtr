"""State-db schema tests: creation, integrity, and the cascade properties."""

import sqlite3

import pytest

from fxtr_experiment.storage import (
    FAILURE_FAULT,
    OBS_KIND_CELL_VALUE,
    SCHEMA_VERSION,
    create_workflow_state_schema,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("PRAGMA foreign_keys = ON")
    create_workflow_state_schema(c)
    return c


def _seed_generation(c: sqlite3.Connection, gen: str = "g1") -> tuple[int, int]:
    """One task grid with one executing generation + linked rows.

    Returns:
        ``(exec_id, version_id)`` — the private keys of the seeded
        generation and the fact it observed.
    """
    c.execute(
        "INSERT INTO grids (key_path, parent_path, kind, config_id) VALUES (?, ?, ?, ?)",
        ('["mh","grades"]', '["mh"]', "task", b"cfg1"),
    )
    c.execute(
        "INSERT INTO grids (key_path, parent_path, kind, config_id) VALUES (?, ?, ?, ?)",
        ('["mh","grades","judge_calls"]', '["mh","grades"]', "checkpoint", b"cfg2"),
    )
    cur = c.execute(
        "INSERT INTO task_execs (generation, grid_path, coords, status,"
        " current_fn, executed_fn) VALUES (?, ?, ?, ?, ?, ?)",
        (gen, '["mh","grades"]', '["s1","j1"]', "running", b"fn", b"fn"),
    )
    exec_id = cur.lastrowid
    assert exec_id is not None
    cur = c.execute(
        "INSERT INTO subject_versions (grid_path, coords, fingerprint) VALUES (?, ?, ?)",
        ('["transcripts"]', '["s1"]', b"v1"),
    )
    version_id = cur.lastrowid
    assert version_id is not None
    c.execute(
        "INSERT INTO observations (exec_id, version_id, kind) VALUES (?, ?, ?)",
        (exec_id, version_id, OBS_KIND_CELL_VALUE),
    )
    c.execute(
        "INSERT INTO checkpoints (grid_path, coords, exec_id, step_type_id,"
        " args_id, value_id) VALUES (?, ?, ?, ?, ?, ?)",
        (
            '["mh","grades","judge_calls"]',
            '["s1","j1","p1"]',
            exec_id,
            b"step",
            b"args",
            b"result",
        ),
    )
    c.execute(
        "INSERT INTO task_results (exec_id, output, value_id) VALUES (?, ?, ?)",
        (exec_id, "scores", b"frame1"),
    )
    c.execute(
        "INSERT INTO grid_assertions (exec_id, version_id) VALUES (?, ?)",
        (exec_id, version_id),
    )
    c.execute(
        "INSERT INTO failures (exec_id, kind, error_class, message, ts)" " VALUES (?, ?, ?, ?, ?)",
        (exec_id, FAILURE_FAULT, "CellFaultError", "no value at x", "2026-07-04T00:00:00"),
    )
    c.commit()
    return exec_id, version_id


def test_schema_version(conn: sqlite3.Connection) -> None:
    (v,) = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert v == str(SCHEMA_VERSION)


def test_generation_turnover_is_one_delete(conn: sqlite3.Connection) -> None:
    """Deleting the exec row (by public generation id) cascades observations/
    checkpoints/results/grid_assertions/failures — the atomic old-state
    cleanup falls out of the schema."""
    _seed_generation(conn)
    tables = ("observations", "checkpoints", "task_results", "grid_assertions", "failures")
    for table in tables:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    with conn:
        conn.execute("DELETE FROM task_execs WHERE generation = 'g1'")
    for table in tables:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    # Version rows are pure interning and survive.
    assert conn.execute("SELECT COUNT(*) FROM subject_versions").fetchone()[0] == 1


def test_one_current_generation_per_cell(conn: sqlite3.Connection) -> None:
    _seed_generation(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_execs (generation, grid_path, coords, status,"
            " current_fn, executed_fn) VALUES (?, ?, ?, ?, ?, ?)",
            ("g2", '["mh","grades"]', '["s1","j1"]', "running", b"fn", b"fn"),
        )


def test_public_generation_id_is_unique(conn: sqlite3.Connection) -> None:
    _seed_generation(conn)
    conn.execute(
        "INSERT INTO grids (key_path, parent_path, kind, config_id) VALUES (?, ?, ?, ?)",
        ('["other"]', "[]", "task", b"cfg3"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_execs (generation, grid_path, coords, status,"
            " current_fn, executed_fn) VALUES (?, ?, ?, ?, ?, ?)",
            ("g1", '["other"]', "[]", "running", b"fn", b"fn"),
        )


def test_floating_versions_per_location(conn: sqlite3.Connection) -> None:
    """One location may hold many interned versions (different observed
    values); the *same* (location, fingerprint) pair is unique."""
    conn.execute(
        "INSERT INTO subject_versions (grid_path, fingerprint) VALUES (?, ?)",
        ('["cfg"]', b"v1"),
    )
    conn.execute(
        "INSERT INTO subject_versions (grid_path, fingerprint) VALUES (?, ?)",
        ('["cfg"]', b"v2"),
    )
    assert conn.execute("SELECT COUNT(*) FROM subject_versions").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO subject_versions (grid_path, fingerprint) VALUES (?, ?)",
            ('["cfg"]', b"v1"),
        )


def test_observation_edges(conn: sqlite3.Connection) -> None:
    """The PK forbids duplicate edges; one-entry-per-(generation, location)
    is deliberately app-level (the in-memory record), so a second version
    of the same location is insertable at the schema level."""
    exec_id, version_1 = _seed_generation(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO observations (exec_id, version_id, kind) VALUES (?, ?, ?)",
            (exec_id, version_1, OBS_KIND_CELL_VALUE),
        )
    cur = conn.execute(
        "INSERT INTO subject_versions (grid_path, coords, fingerprint) VALUES (?, ?, ?)",
        ('["transcripts"]', '["s1"]', b"v2"),
    )
    conn.execute(
        "INSERT INTO observations (exec_id, version_id, kind) VALUES (?, ?, ?)",
        (exec_id, cur.lastrowid, OBS_KIND_CELL_VALUE),
    )


REVERSE_OBSERVERS_SQL = """
    SELECT te.grid_path, te.coords, te.generation
    FROM subject_versions sv
    JOIN observations o ON o.version_id = sv.version_id
    JOIN task_execs te ON te.exec_id = o.exec_id
    WHERE sv.grid_path = ? AND sv.coords = ?
"""


def test_reverse_observer_query_is_index_covered(conn: sqlite3.Connection) -> None:
    """The UI's "who observed this cell" query works and is index-covered:
    location → versions (identity-index prefix scan) → edges
    (observations_by_version) → observer cells (task_execs rowid PK)."""
    _seed_generation(conn)
    rows = conn.execute(REVERSE_OBSERVERS_SQL, ('["transcripts"]', '["s1"]')).fetchall()
    assert rows == [('["mh","grades"]', '["s1","j1"]', "g1")]
    plan = " ".join(
        row[-1]
        for row in conn.execute(
            "EXPLAIN QUERY PLAN " + REVERSE_OBSERVERS_SQL, ('["transcripts"]', '["s1"]')
        )
    )
    assert "subject_versions_identity" in plan
    assert "observations_by_version" in plan


FORWARD_RECORD_SQL = """
    SELECT sv.grid_path, sv.coords, sv.output, sv.fingerprint, o.kind
    FROM task_execs te
    JOIN observations o ON o.exec_id = te.exec_id
    JOIN subject_versions sv ON sv.version_id = o.version_id
    WHERE te.generation = ?
"""


def test_forward_record_query_is_index_covered(conn: sqlite3.Connection) -> None:
    """The record-load query (generation → everything it observed) works and
    is index-covered: generation (UNIQUE index) → edges (the observations
    PK's exec_id prefix) → facts (subject_versions rowid PK)."""
    _seed_generation(conn)
    rows = conn.execute(FORWARD_RECORD_SQL, ("g1",)).fetchall()
    assert rows == [('["transcripts"]', '["s1"]', "", b"v1", OBS_KIND_CELL_VALUE)]
    plan = " ".join(
        row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + FORWARD_RECORD_SQL, ("g1",))
    )
    assert "SCAN" not in plan  # every hop is an index/PK search


def test_target_idempotence(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO targets (slice_id, expected_config, grid_path, coords)"
        " VALUES (?, ?, ?, ?)",
        (b"slice", b"cfg", '["wanted"]', '["a"]'),
    )
    conn.execute(
        "INSERT OR IGNORE INTO targets (slice_id, expected_config, grid_path, coords)"
        " VALUES (?, ?, ?, ?)",
        (b"slice", b"cfg", '["wanted"]', '["a"]'),
    )
    assert conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 1


def test_grid_delete_cascades_contents(conn: sqlite3.Connection) -> None:
    """rmdir-style deletion (v1 spec): the emptiness check is an indexed
    child lookup by parent_path; deletion goes leaves-first, and each
    single-row delete cascades its contents."""
    _seed_generation(conn)

    def children(path: str) -> int:
        return conn.execute("SELECT COUNT(*) FROM grids WHERE parent_path = ?", (path,)).fetchone()[
            0
        ]

    assert children('["mh","grades"]') == 1  # non-empty: delete must refuse
    with conn:
        conn.execute("DELETE FROM grids WHERE key_path = ?", ('["mh","grades","judge_calls"]',))
    assert children('["mh","grades"]') == 0  # now deletable
    with conn:
        conn.execute("DELETE FROM grids WHERE key_path = ?", ('["mh","grades"]',))
    assert conn.execute("SELECT COUNT(*) FROM task_execs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
