# Stage 17: Better Archive Export (LMDB NOSUBDIR + Tar Archives)

Three related changes: (1) new LMDB databases use single-file NOSUBDIR mode, (2) archive export produces compact files with optional tar packaging, (3) archive import handles both tar and raw sqlite inputs.

## 1. LMDB NOSUBDIR Mode for New Databases

**Goal:** New databases create a single `.mdb` file instead of a `.lmdb/` directory, matching how SQLite files behave. Existing databases continue to work.

**Change in `sqlite_ops.py`, `_setup_database_inner()`** (around line 230):

Currently the LMDB path is always `f"{db_path}.data1.lmdb"` with default subdir mode. Replace with detection logic:

```python
old_lmdb_dir = f"{db_path}.data1.lmdb"
if os.path.exists(old_lmdb_dir):
    # Legacy format: directory-based LMDB
    lmdb_env = lmdb.open(old_lmdb_dir, subdir=True, ...)
else:
    # New format: single-file LMDB
    lmdb_env = lmdb.open(f"{db_path}.data1.mdb", subdir=False, ...)
```

All other LMDB parameters (`map_size`, `writemap`, `max_dbs`) stay the same. The named sub-database `b"data1/1"` works identically in both modes.

No migration needed — old databases keep working via the existence check, and new databases automatically get the new format.

## 2. Archive Export Rework

**Goal:** Export produces a compact archive by: building in a temp folder, then copying the SQLite file directly and using `env.copyfd(fd, compact=True)` to produce a compact single-file LMDB. Optionally wraps both files in a tarball.

### Method signature change

`export_archive` gains two parameters:

```python
async def export_archive(self, output_path: Path, *, tar: bool = True, gz: bool = False) -> None:
```

### Export flow

1. Flush + full sync (same as current)
2. Create a `TemporaryDirectory` for building
3. Open `SQLiteFxtrSession` at `{tempdir}/store.sqlite` — this creates the fresh archive DB + LMDB
4. Copy entities pin-by-pin (same as current logic)
5. Close the archive session (releases LMDB env)
6. **Copy step:**
   - Re-open the build LMDB in readonly mode (it will be at `{tempdir}/store.sqlite.data1.mdb` since it's a new DB, so NOSUBDIR format after change #1)
   - If `tar=True`:
     - Create staging dir `{tempdir}/archive/`
     - `shutil.copy2()` the sqlite file to `{staging}/store.sqlite`
     - `env.copyfd(fd, compact=True)` to write `{staging}/store.sqlite.data1.mdb`
     - Create tarball from staging dir contents → `output_path`
     - Tar mode: `w:gz` if `gz=True`, else `w:`
   - If `tar=False`:
     - `shutil.copy2()` the sqlite file to `output_path`
     - `env.copyfd(fd, compact=True)` to write `{output_path}.data1.mdb` (alongside the sqlite)
   - Close the readonly LMDB env
7. Temp dir is cleaned up automatically

### Key detail: re-opening LMDB for copyfd

After the archive session closes, re-open the build LMDB just for copying:

```python
build_lmdb_path = f"{tempdir}/store.sqlite.data1.mdb"
env = lmdb.open(build_lmdb_path, subdir=False, readonly=True, max_dbs=1, ...)
with open(dest_mdb_path, "wb") as f:
    env.copyfd(f.fileno(), compact=True)
env.close()
```

This produces a single `.mdb` file with no empty pages, regardless of how the source was structured.

## 3. CLI Changes

### Export command

Add `--tar/--no-tar` and `--gz` flags:

```python
@app.command("export")
def export_archive(
    path: Annotated[Path, typer.Argument(help="Path to the fxtr store")],
    output_file: Annotated[Path, typer.Argument(help="Output archive file path")],
    tar: Annotated[bool, typer.Option(help="Package as tarball")] = True,
    gz: Annotated[bool, typer.Option(help="Compress tarball with gzip")] = False,
) -> None:
```

Typer auto-generates `--tar` and `--no-tar`. Passes `tar=tar, gz=gz` to `ns.export_archive()`.

### Import command

No new flags. Detection by file extension:
- `.tar` → plain tar
- `.tar.gz` or `.tgz` → gzip tar
- `.sqlite` → raw sqlite (current behavior)
- Other → error with descriptive message

## 4. Archive Import Changes

### Method signature (unchanged)

```python
async def import_archive(self, archive_path: Path) -> None:
```

### Import flow

Detection and unpacking is handled before the existing import logic:

```python
suffix = archive_path.suffixes  # e.g. ['.tar', '.gz'] or ['.sqlite']

if is_tarball(archive_path):
    with TemporaryDirectory() as tempdir:
        # Unpack tarball
        with tarfile.open(archive_path, mode) as tf:
            tf.extractall(tempdir)
        sqlite_path = Path(tempdir) / "store.sqlite"
        if not sqlite_path.exists():
            raise error
        # Open archive session at unpacked path and import
        await self._do_import(sqlite_path)
else:
    # Raw sqlite — existing behavior
    await self._do_import(archive_path)
```

The existing import logic (`_do_import` or inlined) stays the same: open `SQLiteFxtrSession`, read "archive" namespace, copy pins to index namespace with conflict detection.

The LMDB detection logic from change #1 handles finding the companion LMDB file (either `.data1.lmdb/` directory or `.data1.mdb` file).

### Tarball detection helper

```python
def _archive_tar_mode(path: Path) -> str | None:
    """Return tarfile open mode if path looks like a tarball, else None."""
    name = path.name
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "r:gz"
    elif name.endswith(".tar"):
        return "r:"
    return None
```

## 5. Security Note for tarball extraction

Use `tarfile.extractall(filter='data')` to prevent path traversal (Python 3.12+). Only expect `store.sqlite` and `store.sqlite.data1.mdb` — reject unexpected files.

## Implementation Steps

- [x] **Step 1: LMDB NOSUBDIR detection in `sqlite_ops.py`**
  - Modify `_setup_database_inner()` to check for old `.data1.lmdb` dir vs new `.data1.mdb` file
  - Add `import os` if not already present
  - Add `subdir=True` explicitly when using old path (for clarity)
  - Use `subdir=False` with new `.data1.mdb` path

- [x] **Step 2: Test LMDB NOSUBDIR**
  - Test that a fresh database creates `.data1.mdb` (not `.data1.lmdb/`)
  - Test that an existing `.data1.lmdb/` directory is still opened correctly
  - Test that after creating a `.data1.lmdb/` directory manually, the old format is used

- [x] **Step 3: Rework `export_archive` in `git_namespace.py`**
  - Add `tar` and `gz` parameters
  - Implement temp-dir build → copy sqlite → copyfd LMDB → optional tar packaging flow
  - Extract tarball creation into a helper if it gets long

- [x] **Step 4: Rework `import_archive` in `git_namespace.py`**
  - Add tarball detection by extension
  - Unpack to temp dir if tarball
  - Add path traversal protection for tarball extraction
  - Core import logic stays the same

- [x] **Step 5: Update CLI in `git_store_cli.py`**
  - Add `--tar/--no-tar` and `--gz` flags to export command
  - Import command: no new flags, detection is automatic

- [x] **Step 6: Tests for archive export/import**
  - Test export with tar (default) and notar
  - Test export with gz compression
  - Test import from tar, tar.gz, and raw sqlite
  - Test roundtrip: export → import with each format
  - Test that exported LMDB is in NOSUBDIR format (single `.mdb` file)

- [x] **Step 7: Update CODE_OVERVIEW.md and LOCAL_CODE_OVERVIEW.md**
  - Document NOSUBDIR mode for new databases
  - Document new archive format (tar with `store.sqlite` + `store.sqlite.data1.mdb`)
  - Document new CLI flags

## Post-implementation: Refactored export to SQLiteNamespaceBackend

After initial implementation, `export_archive` was moved to `SQLiteNamespaceBackend` so that any user of the sqlite store can export a namespace, not just the git store.

Import was kept on `GitNamespaceBackend` only (as `import_archive_into_backing_store`) because the git store's import has specific semantics: it pins entities in the *index* namespace using `id_string()` as the key, which is different from what a general-purpose import might want (preserving original pin names). The method was renamed to `import_archive_into_backing_store` to make it clear that it populates the backing store rather than modifying the worktree namespace.

**Architecture:**
- `SQLiteNamespaceBackend.export_archive(path, tar=True, gz=False)`: Core export logic — builds archive in temp dir, copies pins, compacts LMDB via `copyfd`, optionally packages as tarball. Includes tqdm progress bar and status messages.
- `GitNamespaceBackend.export_archive(...)`: Flushes TSVs, syncs, then delegates to `self._worktree_ns.export_archive()`.
- `GitNamespaceBackend.import_archive_into_backing_store(path)`: Handles tar detection/unpacking, imports into *index* namespace with id_string keys.
- `_compact_copy_lmdb()` helper lives in `sqlite_namespace.py` (alongside the classes that use it).
- `_archive_tar_mode()` helper lives in `git_namespace.py` (tar format detection is a git store concern).

**Tests:** 4 export tests in `test_sqlite_store.py::TestArchiveExport` (notar, tar, tar.gz, multiple pins). Import roundtrip tests are in `test_git_store/` only.
