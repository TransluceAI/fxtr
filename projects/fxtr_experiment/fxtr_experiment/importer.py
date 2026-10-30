"""The at-rest import loader: the layout of ``notes/at_rest_format.md`` →
a workflow state db, destructively.

Three phases, composed by the user surface (`user_service.py`) — parsing
and verification run entirely *outside* the session lock (the entity
store is append-only, so nothing can tear), and only the destructive
half is one locked transaction:

- :func:`read_layout` (sync; a worker thread): walk the directory,
  validate it is a complete well-formed layout (manifest with the right
  format version, no ``EXPORT_IN_PROGRESS`` marker, every non-dot entry
  a known member, every TSV canonically sorted with the right column
  counts), and parse the raw rows. Errors name the offending file (and
  row) — `errors.ImportFormatError`.
- :func:`verify_and_decode` (async; needs the session for entity loads):
  prove every referenced id resolvable in the shared store (all misses
  collected into one `errors.MissingEntitiesError` before anything is
  written), decode the referenced entities (configs, generation records,
  target slices), and verify every **checked-redundant** column against
  the decoded truth (`grid.toml` kind, generations' uuid/status, target
  coords) plus the cross-row laws (file kind vs grid kind, sourcing's
  grid-before-cell, checkpoint rows' grids present). Produces the
  :class:`ImportBundle` the apply phase consumes.
- :func:`apply_import_body` (sync; run by the session as one locked
  transaction): refuse while running, refuse a non-empty state db unless
  forced (wiped in the same transaction — fact and intent tables only;
  the **events log survives**, and the import appends its own event:
  provenance is history, not state), then insert everything — grids with
  rebuilt ``parent_path`` + ``grid_refs``, generation rows expanded from
  their records (fresh exec_ids, re-interned versions), cells, sourcing,
  targets — with **zero stamps** (everything imports conservatively
  unstamped; verdicts re-derive on demand, `lint` is the
  what-did-I-just-load check).

Outstanding view grants may dangle after an import — the same hazard
`delete_grid` already creates; ops fault at use time; the root grant
survives.
"""

from __future__ import annotations

import datetime
import tomllib
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import TYPE_CHECKING

from fxtr2.data.entity_core import FxtrID

from fxtr_experiment.change_hints import note_change
from fxtr_experiment.encoding import (
    config_to_json,
    encode_coords,
    encode_key_path,
    generation_record_ids,
    parent_key_path,
    validate_grid_key,
)
from fxtr_experiment_task.errors import (
    ImportFormatError,
    MissingEntitiesError,
    NotAtRestError,
    StateNotEmptyError,
)
from fxtr_experiment.export import (
    FORMAT_VERSION,
    GRID_TOML_NAME,
    GRID_TSV_NAMES,
    MANIFEST_NAME,
    MARKER_NAME,
)
from fxtr_experiment_task.model import (
    GenerationRecord,
    GridConfig,
    LiteralCoord,
    SliceTemplate,
    TaskGridConfig,
    rule_reference_paths,
)
from fxtr_experiment.storage import (
    FAILURE_KIND_CODES,
    OBS_KIND_CODES,
    REQ_KIND_CODES,
    id_blob,
)

if TYPE_CHECKING:
    from fxtr_experiment.state_store import WorkflowSession

_TSV_COLUMNS = {"cells.tsv": 2, "generations.tsv": 4, "sourcing.tsv": 2, "targets.tsv": 3}


@dataclass(frozen=True)
class ParsedLayout:
    """The layout's raw rows, straight off the files (phase 1 output).

    Row shapes mirror `export.ExportSnapshot` exactly (canonical identity
    text + FxtrIDs, in file order); nothing has been checked against the
    entity store yet.
    """

    grids: tuple[tuple[str, str, FxtrID], ...]
    cells: tuple[tuple[str, str, FxtrID], ...]
    generations: tuple[tuple[str, str, str, str, FxtrID], ...]
    sourcing: tuple[tuple[str, str, FxtrID], ...]
    targets: tuple[tuple[str, str, FxtrID, FxtrID], ...]


@dataclass(frozen=True)
class ImportBundle:
    """Everything the apply phase needs, fully verified (phase 2 output).

    Attributes:
        layout: The raw rows (see :class:`ParsedLayout`).
        configs: Decoded canonical config per grid key-path.
        rule_refs: Per task grid: the key-path texts its stored rule
            addresses (the ``grid_refs`` rebuild, from the decoded
            configs).
        records: Decoded generation record per (grid_path, coords) —
            the rows the generation expansion inserts.
    """

    layout: ParsedLayout
    configs: dict[str, GridConfig]
    rule_refs: dict[str, tuple[str, ...]]
    records: dict[tuple[str, str], GenerationRecord]


@dataclass(frozen=True)
class ImportReport:
    """Counts per imported fact kind (the whole return of an import)."""

    grids: int
    cells: int
    generations: int
    sourcing_records: int
    targets: int


# ---------------------------------------------------------------------------
# Phase 1: read + parse (sync file I/O; no session, no lock)
# ---------------------------------------------------------------------------


def read_layout(root: Path) -> ParsedLayout:
    """Walk and parse an export directory into raw rows.

    Args:
        root: The export directory (a completed export — the layout of
            ``notes/at_rest_format.md``).

    Returns:
        The raw rows, unverified against the store.

    Raises:
        ImportFormatError: Anything non-layout-shaped — missing/mismatched
            manifest, a present ``EXPORT_IN_PROGRESS`` marker (a torn
            export), unknown entries, bad keys, malformed or unsorted or
            empty TSVs, bad id strings — named with file (and row).
    """
    if not root.is_dir():
        raise ImportFormatError(f"{root} is not a directory")
    if (root / MARKER_NAME).exists():
        raise ImportFormatError(
            f"{root / MARKER_NAME} is present: the export is in progress or"
            " crashed partway — re-export or restore from version control"
        )
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ImportFormatError(f"{manifest_path} is missing: not an export directory")
    try:
        manifest = tomllib.loads(manifest_path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ImportFormatError(f"{manifest_path}: {exc}") from exc
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ImportFormatError(
            f"{manifest_path}: format_version {manifest.get('format_version')!r}"
            f" is not the supported {FORMAT_VERSION}"
        )

    grids: list[tuple[str, str, FxtrID]] = []
    cells: list[tuple[str, str, FxtrID]] = []
    generations: list[tuple[str, str, str, str, FxtrID]] = []
    sourcing: list[tuple[str, str, FxtrID]] = []
    targets: list[tuple[str, str, FxtrID, FxtrID]] = []

    def walk(directory: Path, keys: tuple[str, ...]) -> None:
        for entry in sorted(directory.iterdir()):
            name = entry.name
            if name.startswith("."):
                continue  # invisible, as in export
            if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
                raise ImportFormatError(f"{entry} is not a regular file or directory")
            if entry.is_dir():
                try:
                    validate_grid_key(name)
                except Exception as exc:
                    raise ImportFormatError(f"{entry}: not a valid grid directory") from exc
                child_keys = keys + (name,)
                if not (entry / GRID_TOML_NAME).is_file():
                    raise ImportFormatError(f"{entry}: grid directory without {GRID_TOML_NAME}")
                grid_path = encode_key_path(child_keys)
                grids.append((grid_path, *_parse_grid_toml(entry / GRID_TOML_NAME)))
                for tsv_name in GRID_TSV_NAMES:
                    tsv = entry / tsv_name
                    if tsv.is_file():
                        _parse_tsv(tsv, tsv_name, grid_path, cells, generations, sourcing, targets)
                walk(entry, child_keys)
            elif keys == () and name in (MANIFEST_NAME,):
                continue  # parsed above; the marker was refused above
            elif keys != () and name in (GRID_TOML_NAME, *GRID_TSV_NAMES):
                continue  # parsed with its grid directory
            else:
                raise ImportFormatError(f"{entry} is not part of the export layout")

    walk(root, ())
    return ParsedLayout(
        grids=tuple(grids),
        cells=tuple(cells),
        generations=tuple(generations),
        sourcing=tuple(sourcing),
        targets=tuple(targets),
    )


def _parse_grid_toml(path: Path) -> tuple[str, FxtrID]:
    """Parse one ``grid.toml`` → (kind text, config id).

    Raises:
        ImportFormatError: Bad TOML, wrong key set, or a bad id string.
    """
    try:
        doc = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ImportFormatError(f"{path}: {exc}") from exc
    if set(doc) != {"kind", "config_id"} or not all(isinstance(v, str) for v in doc.values()):
        raise ImportFormatError(f"{path}: expected exactly kind + config_id string keys")
    return doc["kind"], _parse_id(doc["config_id"], path, row=None)


def _parse_id(text: str, path: Path, row: int | None) -> FxtrID:
    """Parse one id-string column; ImportFormatError names file/row."""
    try:
        return FxtrID.from_id_string(text)
    except Exception as exc:
        where = f"{path}" if row is None else f"{path}:{row}"
        raise ImportFormatError(f"{where}: bad entity id {text!r}") from exc


def _parse_tsv(
    path: Path,
    tsv_name: str,
    grid_path: str,
    cells: list[tuple[str, str, FxtrID]],
    generations: list[tuple[str, str, str, str, FxtrID]],
    sourcing: list[tuple[str, str, FxtrID]],
    targets: list[tuple[str, str, FxtrID, FxtrID]],
) -> None:
    """Parse one per-grid TSV into the row accumulators.

    Enforces the canonical file form (maximally strict, like everything
    in the format): right column count per row, rows sorted and unique
    on their full text, no empty files (omit-if-empty is the law).

    Args:
        path: The file (for error naming).
        tsv_name: Which reserved file this is (selects shape and sink).
        grid_path: The owning grid's key-path text (rows are coords-only
            on disk; the accumulators carry absolute rows).
        cells / generations / sourcing / targets: The accumulators.

    Raises:
        ImportFormatError: Any deviation from the canonical form.
    """
    text = path.read_text()
    if text == "":
        raise ImportFormatError(f"{path}: empty (omit-if-empty is the canonical form)")
    if not text.endswith("\n"):
        raise ImportFormatError(f"{path}: missing trailing newline")
    lines = text.splitlines()
    if lines != sorted(set(lines)):
        raise ImportFormatError(f"{path}: rows must be sorted and unique")
    for i, line in enumerate(lines, start=1):
        parts = line.split("\t")
        if len(parts) != _TSV_COLUMNS[tsv_name]:
            raise ImportFormatError(
                f"{path}:{i}: expected {_TSV_COLUMNS[tsv_name]} columns, got {len(parts)}"
            )
        if tsv_name == "cells.tsv":
            cells.append((grid_path, parts[0], _parse_id(parts[1], path, i)))
        elif tsv_name == "generations.tsv":
            generations.append(
                (grid_path, parts[0], parts[1], parts[2], _parse_id(parts[3], path, i))
            )
        elif tsv_name == "sourcing.tsv":
            sourcing.append((grid_path, parts[0], _parse_id(parts[1], path, i)))
        else:
            targets.append(
                (grid_path, parts[0], _parse_id(parts[1], path, i), _parse_id(parts[2], path, i))
            )


# ---------------------------------------------------------------------------
# Phase 2: verify + decode (async; entity loads through the session)
# ---------------------------------------------------------------------------


async def verify_and_decode(session: "WorkflowSession", layout: ParsedLayout) -> ImportBundle:
    """Prove the layout against the shared store and decode its entities.

    Args:
        session: The importing session (its entity store is the shared
            store the layout's ids must resolve in).
        layout: Phase 1's raw rows.

    Returns:
        The fully verified bundle the apply phase consumes.

    Raises:
        MissingEntitiesError: Some referenced ids are not in the store —
            every miss listed (nothing has been written).
        ImportFormatError: A checked-redundant column disagrees with the
            decoded truth, or a cross-row law fails (file kind vs grid
            kind, sourcing's grid-before-cell, a checkpoint row's grid
            absent, an unresolvable target slice).
    """
    # Presence first, everything collected: the direct id columns...
    direct: list[FxtrID] = [cfg for _, _, cfg in layout.grids]
    direct += [v for _, _, v in layout.cells]
    direct += [rid for _, _, _, _, rid in layout.generations]
    direct += [fp for _, _, fp in layout.sourcing]
    for _, _, slice_id, expected in layout.targets:
        direct += [slice_id, expected]
    missing = []
    for entity_id in direct:
        try:
            await session.ensure_loadable(entity_id)
        except KeyError:
            missing.append(entity_id.id_string())
    if missing:
        raise MissingEntitiesError(
            "missing from the entity store: " + ", ".join(sorted(set(missing)))
        )

    # ...then decode, then the record closures (decodable only now).
    configs: dict[str, GridConfig] = {}
    kinds: dict[str, str] = {}
    rule_refs: dict[str, tuple[str, ...]] = {}
    for grid_path, kind_text, config_id in layout.grids:
        entity = await session.load_config(config_id)
        configs[grid_path] = entity.config
        kinds[grid_path] = kind_text
        decoded_kind = config_to_json(entity.config)["kind"]
        if decoded_kind != kind_text:
            raise ImportFormatError(
                f"grid {grid_path}: grid.toml kind {kind_text!r} disagrees with the"
                f" config's {decoded_kind!r}"
            )
        if isinstance(entity.config, TaskGridConfig):
            rule_refs[grid_path] = tuple(
                encode_key_path(keys) for keys in rule_reference_paths(entity.config.rule)
            )

    records: dict[tuple[str, str], GenerationRecord] = {}
    grid_paths = set(kinds)
    for grid_path, coords, gen, status, record_id in layout.generations:
        if kinds[grid_path] != "task":
            raise ImportFormatError(
                f"grid {grid_path}: generations.tsv on a {kinds[grid_path]!r} grid"
            )
        record = (await session.load_generation_record(record_id)).record
        if record.generation != gen or record.status != status:
            raise ImportFormatError(
                f"grid {grid_path}{coords}: generations.tsv row ({gen}, {status})"
                f" disagrees with the record ({record.generation}, {record.status})"
            )
        for referenced in generation_record_ids(record):
            try:
                await session.ensure_loadable(referenced)
            except KeyError:
                missing.append(referenced.id_string())
        for ckpt in record.checkpoints:
            if ckpt.grid_path not in grid_paths:
                raise ImportFormatError(
                    f"grid {grid_path}{coords}: checkpoint row references grid"
                    f" {ckpt.grid_path}, which the layout does not contain"
                )
        records[(grid_path, coords)] = record
    if missing:
        raise MissingEntitiesError(
            "missing from the entity store: " + ", ".join(sorted(set(missing)))
        )

    for grid_path, coords, _v in layout.cells:
        if kinds[grid_path] == "task":
            raise ImportFormatError(f"grid {grid_path}: cells.tsv on a task grid")
    grid_sourced = {(g, c) for g, c, _ in layout.sourcing if c == ""}
    for grid_path, coords, _fp in layout.sourcing:
        if coords != "" and (grid_path, "") not in grid_sourced:
            raise ImportFormatError(
                f"grid {grid_path}: cell sourcing at {coords} without the"
                " grid-level record (the grid-before-cell invariant)"
            )
    for grid_path, coords, slice_id, _expected in layout.targets:
        template = (await session.load_slice_template(slice_id)).template
        resolved_path, resolved_coords = _resolve_target(template)
        if resolved_path != grid_path or resolved_coords != coords:
            raise ImportFormatError(
                f"grid {grid_path}: targets.tsv coords {coords} disagree with the"
                f" slice template ({resolved_path}{resolved_coords})"
            )

    return ImportBundle(layout=layout, configs=configs, rule_refs=rule_refs, records=records)


def _resolve_target(template: SliceTemplate) -> tuple[str, str]:
    """A target slice's denormalized location (fully bound ⇒ all-literal).

    Args:
        template: The stored target slice (root frame).

    Returns:
        ``(grid_path text, flattened coords text)``.

    Raises:
        ImportFormatError: A non-literal template position (targets are
            fully bound; anything else cannot be a target slice).
    """
    coords = []
    for step in template.path:
        for t in step.cell:
            if not isinstance(t, LiteralCoord):
                raise ImportFormatError(f"target slice is not fully bound: {template!r}")
            coords.append(t.value)
    for t in template.target_axes:
        if not isinstance(t, LiteralCoord):
            raise ImportFormatError(f"target slice is not fully bound: {template!r}")
        coords.append(t.value)
    return (
        encode_key_path(tuple(step.key for step in template.path)),
        encode_coords(tuple(coords)),
    )


# ---------------------------------------------------------------------------
# Phase 3: the destructive apply (sync; ONE locked transaction)
# ---------------------------------------------------------------------------


def apply_import_body(conn: Connection, bundle: ImportBundle, force: bool) -> ImportReport:
    """Wipe (if forced) and load the bundle — one transaction.

    Args:
        conn: The state-db connection (run via the session under its
            lock — this body IS the destructive step).
        bundle: Phase 2's verified bundle.
        force: Wipe a non-empty state db instead of refusing. The wipe
            covers the fact and intent tables (and the derived
            memos/interning); the **events log survives** — provenance
            is history, not state.

    Returns:
        Counts per imported fact kind.

    Raises:
        NotAtRestError: A generation is running.
        StateNotEmptyError: The db holds state and ``force`` is False.
    """
    layout = bundle.layout
    with conn:
        if conn.execute("SELECT 1 FROM task_execs WHERE status='running'").fetchone():
            raise NotAtRestError("cannot import: a generation is running")
        occupied = any(
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in ("grids", "task_execs", "cells", "user_sourcing", "targets")
        )
        if occupied and not force:
            raise StateNotEmptyError(
                "the state db is not empty; pass force=True to replace its state"
                " (export first — git is the backup)"
            )
        for table in (
            "task_execs",
            "grids",
            "user_sourcing",
            "targets",
            "wanted_stamps",
            "subject_versions",
        ):
            conn.execute(f"DELETE FROM {table}")  # cascades sweep the generation tables

        for grid_path, kind_text, config_id in layout.grids:
            conn.execute(
                "INSERT INTO grids (key_path, parent_path, kind, config_id)" " VALUES (?, ?, ?, ?)",
                (grid_path, parent_key_path(grid_path), kind_text, id_blob(config_id)),
            )
        for grid_path, refs in bundle.rule_refs.items():
            for ref in refs:
                conn.execute(
                    "INSERT OR IGNORE INTO grid_refs (referrer_path, referenced_path)"
                    " VALUES (?, ?)",
                    (grid_path, ref),
                )
        for grid_path, coords, value_id in layout.cells:
            conn.execute(
                "INSERT INTO cells (grid_path, coords, value_id) VALUES (?, ?, ?)",
                (grid_path, coords, id_blob(value_id)),
            )
        for grid_path, coords, _gen, _status, record_id in layout.generations:
            record = bundle.records[(grid_path, coords)]
            _insert_generation_from_record(conn, grid_path, coords, record, record_id)
        for grid_path, coords, fingerprint in layout.sourcing:
            conn.execute(
                "INSERT INTO user_sourcing (grid_path, coords, fingerprint) VALUES (?, ?, ?)",
                (grid_path, coords, id_blob(fingerprint)),
            )
        for grid_path, coords, slice_id, expected in layout.targets:
            conn.execute(
                "INSERT INTO targets (slice_id, expected_config, grid_path, coords)"
                " VALUES (?, ?, ?, ?)",
                (id_blob(slice_id), id_blob(expected), grid_path, coords),
            )
        report = ImportReport(
            grids=len(layout.grids),
            cells=len(layout.cells),
            generations=len(layout.generations),
            sourcing_records=len(layout.sourcing),
            targets=len(layout.targets),
        )
        conn.execute(
            "INSERT INTO events (ts, kind, payload) VALUES (?, 'import', ?)",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(), repr(report)),
        )
        # Events: everything changed — the coarse pair covers the whole
        # refetch surface (structure + counts via "grids", intent lists
        # via "intent"); per-grid precision is pointless here.
        note_change("grids")
        note_change("intent")
        return report


def _insert_generation_from_record(
    conn: Connection, grid_path: str, coords: str, record: GenerationRecord, record_id: FxtrID
) -> None:
    """Expand one decoded record into its generation-scoped rows.

    Fresh ``exec_id`` (autoincrement), re-interned subject versions, no
    stamps (I-G3/I1: everything imports unstamped); failure rows in the
    record's order (insertion order is display order); the row points at
    ``record_id`` (the flip-time invariant holds by construction — the
    rows ARE the record).

    Args:
        conn: The state-db connection (inside the apply transaction).
        grid_path: The task grid's key-path text.
        coords: The cell's flattened-coords text.
        record: The decoded generation record.
        record_id: The record entity's id (the generations.tsv column,
            already verified against ``record``).
    """
    cur = conn.execute(
        "INSERT INTO task_execs (generation, grid_path, coords, status, current_fn,"
        " executed_fn, record_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            record.generation,
            grid_path,
            coords,
            record.status,
            id_blob(record.current_fn),
            id_blob(record.executed_fn),
            id_blob(record_id),
        ),
    )
    exec_id = cur.lastrowid
    for f in record.failures:
        conn.execute(
            "INSERT INTO failures (exec_id, kind, error_class, message, ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (exec_id, FAILURE_KIND_CODES[f.kind], f.error_class, f.message, f.ts),
        )
    for name, value_id in record.results.items():
        conn.execute(
            "INSERT INTO task_results (exec_id, output, value_id) VALUES (?, ?, ?)",
            (exec_id, name, id_blob(value_id)),
        )
    for o in record.observations:
        version_id = _intern(conn, o.grid_path, o.coords, o.output, o.fingerprint)
        conn.execute(
            "INSERT INTO observations (exec_id, version_id, kind) VALUES (?, ?, ?)",
            (exec_id, version_id, OBS_KIND_CODES[o.kind]),
        )
    for a in record.assertions:
        version_id = _intern(conn, a.grid_path, "", "", a.config_id)
        conn.execute(
            "INSERT INTO grid_assertions (exec_id, version_id) VALUES (?, ?)",
            (exec_id, version_id),
        )
    for r in record.request_edges:
        conn.execute(
            "INSERT INTO request_edges (exec_id, grid_path, coords, kind) VALUES (?, ?, ?, ?)",
            (exec_id, r.grid_path, r.coords, REQ_KIND_CODES[r.kind]),
        )
    for c in record.checkpoints:
        conn.execute(
            "INSERT INTO checkpoints (grid_path, coords, exec_id, step_type_id, args_id,"
            " value_id) VALUES (?, ?, ?, ?, ?, ?)",
            (
                c.grid_path,
                c.coords,
                exec_id,
                id_blob(c.step_type_id),
                id_blob(c.args_id),
                id_blob(c.value_id),
            ),
        )


def _intern(conn: Connection, grid_path: str, coords: str, output: str, fp: FxtrID) -> int:
    """Re-intern one subject version; returns its (fresh) version_id."""
    conn.execute(
        "INSERT OR IGNORE INTO subject_versions (grid_path, coords, output, fingerprint)"
        " VALUES (?, ?, ?, ?)",
        (grid_path, coords, output, id_blob(fp)),
    )
    row = conn.execute(
        "SELECT version_id FROM subject_versions WHERE grid_path = ? AND coords = ?"
        " AND output = ? AND fingerprint = ?",
        (grid_path, coords, output, id_blob(fp)),
    ).fetchone()
    return row[0]
