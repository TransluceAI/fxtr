"""The at-rest export writer: workflow state → the layout of
``notes/at_rest_format.md``.

Three pieces, composed by the user surface (`user_service.py`):

- the **snapshot body** (:func:`export_snapshot_body` — a sync body the
  session runs as one locked op, so the five row sets are one coherent
  at-rest fact; it refuses while anything runs, and the flip-time
  invariant guarantees every enumerated generation has its record id);
- the **validator** (:func:`validate_export_dir`): the export dir is
  wholly export-owned, and ownership is *proved*, never assumed — every
  non-dot entry must match the known layout (manifest, marker, per-grid
  reserved files, valid-key directories) or the whole operation refuses
  before touching anything. Dot-entries (``.DS_Store`` and friends drop
  uninvited) are invisible: never validated, never removed (except with
  a removed grid directory), never written.
- the **writer** (:func:`write_export`): deterministic full rebuild —
  same snapshot ⇒ identical bytes (tomlkit documents built fresh in a
  fixed order; TSV rows sorted; trailing newlines) — bracketed by the
  ``EXPORT_IN_PROGRESS`` marker (written first, removed last, its text a
  human-readable explanation), so a crashed export is legible on disk
  and never mistaken for a complete one. Known files/dirs that the
  snapshot no longer wants are removed; a re-export of an unchanged
  state is a no-op git diff.

File I/O is sync (the caller runs it in a worker thread, outside the
session lock — the lock protects the snapshot, not the writes).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection

import tomlkit

from fxtr2.data.entity_core import FxtrID

from fxtr_experiment.encoding import decode_key_path, validate_grid_key
from fxtr_experiment_task.errors import EncodingError, ExportLayoutError, NotAtRestError
from fxtr_experiment.storage import id_of

FORMAT_VERSION = 1

MANIFEST_NAME = "manifest.toml"
MARKER_NAME = "EXPORT_IN_PROGRESS"
GRID_TOML_NAME = "grid.toml"
GRID_TSV_NAMES = ("cells.tsv", "generations.tsv", "sourcing.tsv", "targets.tsv")

_MARKER_TEXT = """\
An export of workflow state into this directory is either in progress or
crashed partway through. If no export is currently running, this
directory's content is incomplete: re-run the export, or restore the
directory from version control. (This file is written first and removed
last by every export — see notes/at_rest_format.md.)
"""


@dataclass(frozen=True)
class ExportSnapshot:
    """One coherent at-rest read of every exported fact (raw row tuples,
    canonical identity text + FxtrIDs; produced under one session lock
    hold by `WorkflowSession.export_snapshot`).

    Attributes:
        grids: ``(key_path, kind, config_id)`` rows.
        cells: ``(grid_path, coords, value_id)`` rows.
        generations: ``(grid_path, coords, generation, status,
            record_id)`` rows — terminal only (the snapshot refused
            otherwise), so ``record_id`` is total.
        sourcing: ``(grid_path, coords, fingerprint)`` rows.
        targets: ``(grid_path, coords, slice_id, expected_config)`` rows.
    """

    grids: tuple[tuple[str, str, FxtrID], ...]
    cells: tuple[tuple[str, str, FxtrID], ...]
    generations: tuple[tuple[str, str, str, str, FxtrID], ...]
    sourcing: tuple[tuple[str, str, FxtrID], ...]
    targets: tuple[tuple[str, str, FxtrID, FxtrID], ...]


@dataclass(frozen=True)
class ExportReport:
    """Counts per exported fact kind (the whole return of an export)."""

    grids: int
    cells: int
    generations: int
    sourcing_records: int
    targets: int


def export_snapshot_body(conn: Connection) -> ExportSnapshot:
    """The locked snapshot body (run via the session, one transaction).

    Args:
        conn: The state-db connection, passed by
            ``WorkflowSession._run`` — this body must only ever execute
            under the session lock (it is one logical op; the lock is
            what makes the five row sets a single coherent snapshot).

    Returns:
        Every exported fact as raw row tuples, each set in its canonical
        file order (see :class:`ExportSnapshot`).

    Raises:
        NotAtRestError: A generation is running — the layout is an
            at-rest format, refused atomically with the read.
    """
    running = conn.execute(
        "SELECT grid_path, coords FROM task_execs WHERE status = 'running'"
    ).fetchone()
    if running is not None:
        raise NotAtRestError(
            f"cannot export: {running[0]}{running[1]} is running (the layout is"
            " an at-rest format)"
        )
    grids = tuple(
        (path, kind, id_of(cfg))
        for path, kind, cfg in conn.execute(
            "SELECT key_path, kind, config_id FROM grids ORDER BY key_path"
        ).fetchall()
    )
    cells = tuple(
        (g, c, id_of(v))
        for g, c, v in conn.execute(
            "SELECT grid_path, coords, value_id FROM cells ORDER BY grid_path, coords"
        ).fetchall()
    )
    generations = []
    for g, c, gen, status, rid in conn.execute(
        "SELECT grid_path, coords, generation, status, record_id FROM task_execs"
        " ORDER BY grid_path, coords"
    ).fetchall():
        # Total by the flip-time invariant (record_id non-NULL ⇔ terminal,
        # and running rows were refused above).
        assert rid is not None
        generations.append((g, c, gen, status, id_of(rid)))
    sourcing = tuple(
        (g, c, id_of(fp))
        for g, c, fp in conn.execute(
            "SELECT grid_path, coords, fingerprint FROM user_sourcing ORDER BY grid_path, coords"
        ).fetchall()
    )
    targets = tuple(
        (g, c, id_of(s), id_of(e))
        for g, c, s, e in conn.execute(
            "SELECT grid_path, coords, slice_id, expected_config FROM targets"
            " ORDER BY grid_path, coords, slice_id"
        ).fetchall()
    )
    return ExportSnapshot(
        grids=grids,
        cells=cells,
        generations=tuple(generations),
        sourcing=sourcing,
        targets=targets,
    )


def validate_export_dir(root: Path) -> None:
    """Prove the directory is export-owned before anything is touched.

    Absent or empty directories qualify; otherwise every non-dot entry
    must be a known layout member (see the module docstring). Symlinks
    and special files always refuse.

    Args:
        root: The export target directory (need not exist; its parents
            need not exist either — the writer creates them).

    Raises:
        ExportLayoutError: The first non-export-shaped entry found, or
            ``root`` exists and is not a directory. Guaranteed to have
            modified nothing.
    """
    if not root.exists():
        return
    if not root.is_dir():
        raise ExportLayoutError(f"export target {root} exists and is not a directory")
    _validate_level(root, is_root=True)


def _validate_level(directory: Path, *, is_root: bool) -> None:
    """Validate one directory level of the layout (recursing into grids).

    Args:
        directory: An existing directory to check (``root`` itself, or a
            grid directory nested anywhere below it).
        is_root: Whether ``directory`` is the export root — the root
            allows exactly the manifest and the marker as files; every
            other level allows exactly the per-grid reserved files.
            Subdirectories at any level must be valid-key grid dirs.

    Raises:
        ExportLayoutError: The first non-export-shaped entry found.
    """
    allowed_files = {MANIFEST_NAME, MARKER_NAME} if is_root else {GRID_TOML_NAME, *GRID_TSV_NAMES}
    for entry in sorted(directory.iterdir()):
        if entry.name.startswith("."):
            continue  # macOS et al. drop these uninvited; invisible to us
        if entry.is_symlink():
            raise ExportLayoutError(f"{entry} is a symlink; refusing to own it")
        if entry.is_file():
            if entry.name not in allowed_files:
                raise ExportLayoutError(
                    f"{entry} is not part of the export layout; refusing to own"
                    " this directory (exports rebuild their directory wholly —"
                    " keep unrelated files outside it)"
                )
        elif entry.is_dir():
            try:
                validate_grid_key(entry.name)
            except EncodingError as exc:
                raise ExportLayoutError(
                    f"{entry} is not a valid grid directory; refusing to own" " this directory"
                ) from exc
            _validate_level(entry, is_root=False)
        else:
            raise ExportLayoutError(f"{entry} is not a regular file or directory")


def _grid_dir(root: Path, key_path: str) -> Path:
    """The grid's directory under the export root.

    Args:
        root: The export root directory.
        key_path: The grid's key-path text (canonical JSON array of
            keys, as stored in ``grids.key_path``). Keys are
            filesystem-safe by the creation-time key-charset law, so
            each becomes one directory component verbatim.

    Returns:
        ``root / key₁ / … / keyₙ`` (the export root itself for the
        empty root path).
    """
    d = root
    for key in decode_key_path(key_path):
        d = d / key
    return d


def _tsv(rows: list[tuple[str, ...]]) -> bytes:
    """Render one TSV file's bytes.

    Args:
        rows: One tuple of already-rendered text columns per row —
            canonical identity text (TSV-safe: canonical JSON escaping
            forbids raw tabs/newlines) and ``id_string()`` forms; the
            caller renders, this function only joins. Any order: rows
            are sorted here (whole-row lexicographic, which the leading
            key column(s) dominate), giving the canonical byte form.

    Returns:
        Headerless tab-separated bytes, one ``\\n``-terminated line per
        row.
    """
    return "".join("\t".join(row) + "\n" for row in sorted(rows)).encode()


def _grid_toml(kind: str, config_id: FxtrID) -> bytes:
    """Render one ``grid.toml`` (the grid row: checked-redundant kind +
    the canonical config entity id).

    Args:
        kind: The grid's kind text, exactly as stored in ``grids.kind``.
        config_id: The canonical config entity id (``grids.config_id``).

    Returns:
        Deterministic TOML bytes (fresh document, fixed key order).
    """
    doc = tomlkit.document()
    doc["kind"] = kind
    doc["config_id"] = config_id.id_string()
    return tomlkit.dumps(doc).encode()


def _manifest_toml() -> bytes:
    """Render ``manifest.toml`` (the format version, nothing else).

    Returns:
        Deterministic TOML bytes.
    """
    doc = tomlkit.document()
    doc["format_version"] = FORMAT_VERSION
    return tomlkit.dumps(doc).encode()


def write_export(snapshot: ExportSnapshot, root: Path) -> ExportReport:
    """Rebuild ``root`` as the snapshot's at-rest layout (deterministic).

    Validate → write the marker → write every desired file → remove
    known files/dirs the snapshot no longer wants → remove the marker.
    A crash leaves the marker in place (its text explains the state; git
    is the recovery path); re-export over a crashed dir is safe (the
    marker is layout, and everything is rewritten).

    Sync file I/O throughout — run in a worker thread, and never under
    the session lock (the snapshot already happened).

    Args:
        snapshot: The facts to write (one coherent session snapshot —
            `WorkflowSession.export_snapshot`).
        root: The export target directory (created, parents included,
            if absent; must be provably export-owned otherwise).

    Returns:
        Counts per exported fact kind.

    Raises:
        ExportLayoutError: The directory is not provably export-owned
            (nothing has been touched).
    """
    validate_export_dir(root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / MARKER_NAME
    marker.write_text(_MARKER_TEXT)

    # Desired file map (relative path → bytes), grouped per grid.
    desired: dict[Path, bytes] = {Path(MANIFEST_NAME): _manifest_toml()}
    desired_dirs: set[Path] = set()
    per_grid: dict[str, dict[str, list[tuple[str, ...]]]] = {}
    for g, c, v in snapshot.cells:
        per_grid.setdefault(g, {}).setdefault("cells.tsv", []).append((c, v.id_string()))
    for g, c, gen, status, rid in snapshot.generations:
        per_grid.setdefault(g, {}).setdefault("generations.tsv", []).append(
            (c, gen, status, rid.id_string())
        )
    for g, c, fp in snapshot.sourcing:
        per_grid.setdefault(g, {}).setdefault("sourcing.tsv", []).append((c, fp.id_string()))
    for g, c, s, e in snapshot.targets:
        per_grid.setdefault(g, {}).setdefault("targets.tsv", []).append(
            (c, s.id_string(), e.id_string())
        )
    for key_path, kind, config_id in snapshot.grids:
        grid_dir = _grid_dir(root, key_path).relative_to(root)
        desired_dirs.add(grid_dir)
        desired[grid_dir / GRID_TOML_NAME] = _grid_toml(kind, config_id)
        for tsv_name, rows in per_grid.get(key_path, {}).items():
            desired[grid_dir / tsv_name] = _tsv(rows)  # omit-if-empty: absent keys

    for relpath, data in desired.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    _prune(root, Path(), desired, desired_dirs, is_root=True)
    marker.unlink()
    return ExportReport(
        grids=len(snapshot.grids),
        cells=len(snapshot.cells),
        generations=len(snapshot.generations),
        sourcing_records=len(snapshot.sourcing),
        targets=len(snapshot.targets),
    )


def _prune(
    root: Path,
    rel: Path,
    desired: dict[Path, bytes],
    desired_dirs: set[Path],
    *,
    is_root: bool,
) -> None:
    """Remove known layout members the snapshot no longer wants (the
    validator already proved everything non-dot is a layout member).

    Args:
        root: The export root directory.
        rel: The level being pruned, relative to ``root`` (``Path()``
            for the root itself; recursion appends one grid key per
            level).
        desired: The full desired file map, ``root``-relative paths →
            bytes (already written when this runs) — files not in it
            are removed.
        desired_dirs: Every ``root``-relative grid directory the
            snapshot wants — directories not in it are removed whole
            (``rmtree``: their dot-entries go with them).
        is_root: Whether this level is the export root — only there is
            the marker expected (and skipped: the caller removes it
            last, as the bracket).
    """
    for entry in sorted((root / rel).iterdir()):
        name = entry.name
        if name.startswith("."):
            continue
        entry_rel = rel / name
        if entry.is_dir():
            if entry_rel in desired_dirs:
                _prune(root, entry_rel, desired, desired_dirs, is_root=False)
            else:
                shutil.rmtree(entry)  # a validated grid dir (dot-entries and all)
        elif is_root and name == MARKER_NAME:
            continue  # the bracket; removed by the caller as the last step
        elif entry_rel not in desired:
            entry.unlink()
