# Stage 17 - Better Archive Export: Scratch Notes

## Current State

### LMDB Opening (`sqlite_ops.py:230-239`)
```python
lmdb_path = f"{db_path}.data1.lmdb"
lmdb_env = lmdb.open(
    lmdb_path,
    map_size=1024 * 1024 * 1024 * 100,  # 100GB
    writemap=True,
    max_dbs=1,
)
lmdb_data_db = lmdb_env.open_db(b"data1/1", create=True)
```

This always creates a **directory** at `{db_path}.data1.lmdb/` containing `data.mdb` and `lock.mdb`.

### Archive Export (`git_namespace.py:592-629`)
Creates a full `SQLiteFxtrSession` at output_path, copies entities pin-by-pin. This creates both a `.sqlite` file AND a `.data1.lmdb/` directory at the output location.

### Archive Import (`git_namespace.py:631-698`)
Opens archive via `SQLiteFxtrSession.open()`, iterates pins, copies into index namespace.

## py-lmdb API Notes

- `lmdb.open(path, subdir=True)` (default) → directory mode
- `lmdb.open(path, subdir=False)` → single file mode (MDB_NOSUBDIR), lock file at `{path}-lock`
- `env.copy(path, compact=True)` → always creates subdir-style copy (directory)
- `env.copyfd(fd, compact=True)` → writes raw single file, openable with `subdir=False`

## Design Decisions

### LMDB Path Detection
- Check `os.path.exists(f"{db_path}.data1.lmdb")` for old format
- New format: `f"{db_path}.data1.mdb"` with `subdir=False`
- `os.path.exists` works fine here since old path is a directory and new path uses different extension

### Archive Export Flow (tar=True, default)
1. Create temp dir (TemporaryDirectory)
2. Build archive in `{tempdir}/build/store.sqlite` using existing SQLiteFxtrSession logic
3. Close session (closes LMDB too)
4. Create `{tempdir}/archive/` staging area
5. `shutil.copy2(build/store.sqlite, archive/store.sqlite)`
6. Re-open LMDB at `build/store.sqlite.data1.mdb` (NOSUBDIR, since new DB)
7. `env.copyfd(fd, compact=True)` → `archive/store.sqlite.data1.mdb`
8. Close LMDB env
9. Create tar from `archive/` contents → output_path
10. Clean up temp dir

### Archive Export Flow (tar=False)
Same as above but skip staging area, copy directly:
5. `shutil.copy2(build/store.sqlite, output_path)`
6. Re-open LMDB, `copyfd` → `{output_path}.data1.mdb`

### Tar Format
- Default: plain `.tar`
- With `--gz` flag: `.tar.gz`
- Files inside tar: `store.sqlite`, `store.sqlite.data1.mdb` (flat, no subdirectory)

### Import Detection
- `.tar` → plain tar
- `.tar.gz` or `.tgz` → gzip tar
- `.sqlite` → raw sqlite
- Anything else → error with message

### Import Flow (tarball)
1. Detect tarball by extension
2. Unpack to temp dir
3. Find `store.sqlite` in unpacked contents
4. Open `SQLiteFxtrSession` at that path (LMDB will be found via new detection logic)
5. Import entities using existing logic
6. Clean up temp dir

### Import Flow (raw sqlite)
Same as current, no change needed. The LMDB detection logic handles both old and new formats.

## Edge Cases
- The build step creates a fresh LMDB, so after change #1, it will use NOSUBDIR format
- Re-opening the LMDB for copyfd: need `max_dbs=1` and don't need `writemap` (readonly)
- `copyfd` is a blocking call — should be fine since it's in a sync context (no async needed)
- Tar creation: use Python's `tarfile` module, straightforward
- For import, we need to handle the case where the tarball was created with gz compression

## Files to Change
1. `sqlite_ops.py` — LMDB opening logic
2. `git_namespace.py` — export_archive, import_archive methods
3. `git_store_cli.py` — CLI flags (--notar, --gz)
4. Tests for each change
