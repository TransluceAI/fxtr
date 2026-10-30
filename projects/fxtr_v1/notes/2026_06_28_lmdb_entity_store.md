# LMDB-backed entity store

Status: plan / design. Author: pairing session 2026-06-28.

## Problem

The active entity store keeps one CID-addressed `.cbor` file per entity under
`<data.active>/entities/bafyrei/XX/YY/<rest>.cbor` (`ClosedBinaryCBOROnDiskLocator` in
`fxtr/object_model/locator.py`). For large runsets this is **millions of tiny files** — the 5–10 GB
stores hold ~1M entities, and the big ones approach ~100 GB / ~100M entities. That many inodes
exhausts the filesystem (running out of inodes well before running out of bytes) and makes every
directory walk / backup / `rsync` painful.

We want to store entity data in a single **LMDB** database instead, at least for stores driven
through the session manager. LMDB is a memory-mapped B-tree key/value store: one file (plus a lock
file), no per-entity inode, fast point lookups, single-writer / multi-reader MVCC.

## Goals

1. An LMDB-backed entity store that is a drop-in for `ClosedBinaryCBOROnDiskLocator` on both the read
   path (already abstract via `Locator`) and the write path.
2. A `[data]` config switch selecting the entity-store format; default unchanged (`cbor_files`).
3. The session manager (and the backend runner, and CLI read helpers) honor the format for **all**
   entity reads and writes, routed through one factory.
4. A fast, resumable migration script: old `entities/` dir → LMDB. Hard cutover (operator points the
   config at the migrated store; no live dual-format fallback).

Non-goals: changing the runset / run-graph cache storage (stays `active.sqlite`); changing the
on-the-wire CBOR encoding of entities (LMDB values are the exact same bytes as today's `.cbor`
files); migrating v2 (`v2/` is unrelated and out of scope).

## Confirmed decisions (with Daniel)

- **map_size default 512 GB.** Just address space (sparse on APFS/ext4), cheap to over-provision.
  If a store outgrows it, bump on reopen (see "map_size growth" below).
- **writemap off in live processes, on only in the migrator.** Live sessions favor crash-safety;
  the migrator favors throughput.
- **Hard cutover.** No `UnionLocator(lmdb, cbor)` read fallback. `get_active_data_locator` returns a
  single locator based on the configured format. Migration is an explicit, offline step.
- **Trust path-derived CIDs in the migrator.** The CID is recovered from the file path (the inverse
  of `_get_file_path`); we do **not** re-hash file contents to verify. (Content-addressing makes
  writes idempotent anyway, so re-running is safe.)
- **Migrator sorts keys and writes per shard with `MDB_APPEND`** for max write throughput.

## Constraint: `from_locator` must stay picklable

`RunGraphCacheBuilder` stage 1 ships the locator to a `ProcessPoolExecutor`
(`_build_worker_init` / `_build_worker_extract_chunk` in `session_manager.py`), and
`CachingEntityLoader.from_locator` is pickled to get there. `ClosedBinaryCBOROnDiskLocator` is a
frozen dataclass holding only a `Path`, so it pickles trivially.

An open `lmdb.Environment` is **not** picklable, and the same env handle must not be shared across a
`fork`. So the LMDB locator's picklable state is **only the path + open params**; the env is opened
**lazily and cached per-PID**, reopened if `os.getpid()` changed. This is the single most important
correctness detail; it gets a dedicated test exercising the process-pool path.

## Design

### 1. Writable-locator abstraction (`locator.py`)

Reads are already abstract through `Locator`. Writes currently assume the concrete
`ClosedBinaryCBOROnDiskLocator`. Introduce a small ABC so the two backends are interchangeable on
the write path:

```
class WritableLocator(Locator, abc.ABC):
    @abc.abstractmethod
    def write_entity_data(self, data, cid=None) -> None: ...
    # save_entity is identical for all backends -> concrete here:
    def save_entity(self, entity) -> None:
        # walk subgraph (RecursiveSubgraphCollector, include_if = not has_entity),
        # write_entity_data each. (lifted verbatim from ClosedBinaryCBOROnDiskLocator)
```

`ClosedBinaryCBOROnDiskLocator` and the new `LMDBEntityLocator` both subclass it. `save_entity`
moves up into the ABC (both backends share it); only `has_entity` / `retrieve` / `write_entity_data`
differ.

Then retype the three write-path annotations from `ClosedBinaryCBOROnDiskLocator` to
`WritableLocator`:
- `MaterializationWorkerManager.__init__` param (`session_manager.py:2654`)
- the `run_data_locator` local in `SessionManager.create()` (`session_manager.py:3386`)
- `BackendRunner._run_data_locator` (`backend_runner.py:616`)

No call-site logic changes — they only call `save_entity` / `write_entity_data` / `has_entity`.

### 2. `LMDBEntityLocator` (`locator.py`)

A complete-subgraph store, so (like the file locator) `retrieve` returns `(data, self)`.

- **State (picklable):** `path: Path`, `map_size: int`, `writemap: bool`. NOT a frozen dataclass
  (it carries a cached env handle); implement `__getstate__`/`__setstate__` to drop the env.
- **Env access:** `_env()` opens lazily and caches keyed by `os.getpid()`:
  ```
  env = lmdb.Environment(str(path), map_size=map_size, writemap=writemap,
                         max_dbs=1, subdir=True)   # single default sub-db; readers + writer share
  ```
  (`subdir=True` -> the path is a directory holding `data.mdb` + `lock.mdb`; this is fine, it's 2
  inodes total, not millions.)
- **Key/value:** key = `bytes(cid)` (raw CID bytes — compact, fixed width); value =
  `cbor_util.encode_cbor(data)` (exact bytes that are in the `.cbor` files today).
- `has_entity(cid)`: read txn, `txn.get(key) is not None` (or a cursor `set_key`).
- `retrieve(cid)`: read txn `get`, `cbor_util.decode_cbor(value)`, return `(data, self)`.
- `write_entity_data(data, cid=None)`: compute/verify CID exactly as the file locator does (same
  CID-mismatch ValueError), then one write txn `put(key, encoded, overwrite=True)` (idempotent).
- `save_entity` (inherited): batches a whole subgraph — override to wrap the subgraph walk in a
  **single write txn** rather than one txn per entity (the file locator does one file write each;
  for LMDB one txn per `save_entity` call is the right granularity and much faster).
- `metadata()`: `{"type": ..., "path": ...}`.
- `advertised_entities()`: leave empty (same as the file locator — it doesn't enumerate).

**map_size growth:** if a live write raises `lmdb.MapFullError`, surface a clear error telling the
operator to raise `data.lmdb_map_size_gb` and restart. (Optional later: auto `set_mapsize` on a
fresh env. Out of scope for v1; the 512 GB default covers current stores.)

### 3. Config (`fxtrconfig.py`)

Add to `DataConfig`:
```
entity_store_format: Literal["cbor_files", "lmdb"] = "cbor_files"
lmdb_map_size_gb: int = 512
```
Add a path helper:
```
def get_active_entity_store_lmdb(config) -> Path:
    return Path(config.data.active) / "entities.lmdb"
```
(Distinct path from `entities/`, so a migrated store and the old dir can coexist on disk during the
cutover even though only one is read at runtime.)

### 4. Single factory (`api_lib.get_active_data_locator`)

Make this the **one** place that decides the backend:
```
def get_active_data_locator(config) -> locator.WritableLocator:
    if config.data.entity_store_format == "lmdb":
        path = fxtrconfig.get_active_entity_store_lmdb(config)
        path.mkdir(parents=True, exist_ok=True)
        return locator.LMDBEntityLocator(path, map_size=config.data.lmdb_map_size_gb * GiB)
    path = fxtrconfig.get_active_entity_store(config)
    path.mkdir(parents=True, exist_ok=True)
    return locator.ClosedBinaryCBOROnDiskLocator(path)
```
Route the inline constructions through it:
- `SessionManager.create()` -> `api_lib.get_active_data_locator(config)` instead of building
  `ClosedBinaryCBOROnDiskLocator` directly.
- `BackendRunner.__init__` -> same.
- `browse_entities.py` -> build from config via the factory instead of hardcoding the file locator
  (minor; keeps the debug UI working against lmdb stores).

Existing read-only callers of `get_active_data_locator` (e.g. `get_run_ancestors`, the oldest-format
runset migration in `_load_runset_from_path`) then transparently honor the format.

### 5. Migration script (`scripts/migrate_entities_to_lmdb.py`)

Standalone, uv-shebang style like `migrate_runset_cache.py`. Args: `config` (for paths/map_size),
optional `--source` (default `<data.active>/entities`), optional `--dest` (default
`<data.active>/entities.lmdb`), `--map-size-gb`, `--batch-size`.

Performance plan (target: ~100 GB / ~100M tiny files):

- The bottleneck is the **source**: walking + reading millions of inodes. That's inherent to the
  format we're escaping; we parallelize it.
- **Per-shard, sorted, append-mode** writes:
  - Iterate top-level shards `entities/bafyrei/XX/` (and `YY/` sub-shards) one at a time. This bounds
    memory and gives natural resumability checkpoints.
  - Within a shard: a thread pool reads file bytes (I/O-bound, GIL released during read); recover
    each CID from its path (inverse of `_get_file_path`: `cid_str = "bafyrei" + XX + YY + stem`,
    `cbrrr.CID.decode(cid_str)`); collect `(key=bytes(cid), value=bytes)` pairs.
  - **Sort the shard's pairs by key**, then write them in one (or few) txn(s) with
    `cursor.putmulti(pairs, append=True)`. `MDB_APPEND` skips B-tree rebalancing — only valid for
    strictly increasing keys, which the sort guarantees *within a shard*. Shards are processed in
    sorted order too, so keys are globally increasing across shards -> append stays valid end-to-end.
  - Migrator env opens with `writemap=True, map_async=True, sync=False` (metasync off) for speed;
    one `env.sync()` at the very end.
- **Resumable:** record completed shard prefixes in a small sidecar (e.g. `<dest>.progress` json, or
  a dedicated key in a second sub-db). On restart, skip shards already marked done. (Append-mode +
  skip-done means we never re-touch a finished shard; a half-finished shard is redone — safe because
  content-addressed, but to keep `append=True` valid we only mark a shard done after its txn commits,
  and a redo of a partially-written shard must either start a fresh non-append pass for that shard or
  delete its partial keys first. Simplest: checkpoint **per fully-committed shard**, and on a redo of
  the in-flight shard, fall back to `append=False` putmulti for just that shard.)
- Reports counts; **does not** delete or modify the source. Operator cuts over by setting
  `entity_store_format = "lmdb"` in the config (and may delete `entities/` afterward).

### 6. Tests

- Round-trip: write entities via `LMDBEntityLocator`, read back through `CachingEntityLoader`.
- Parity: build an lmdb store and a cbor-file store from the same entity set; assert
  `load_entity_from_locator` returns equal graphs from both.
- Pickle / process-pool: pickle a loader built `from_locator(LMDBEntityLocator(...))`, unpickle in a
  child process, retrieve — exercises the per-PID lazy env.
- Migration: tiny fixture `entities/` dir -> migrate -> assert every CID present and decodes equal.

## Test setup

The full suite has a known dependency gap (documented in `pyproject.toml`): the `demo_09_15_*`
example packages are commented out of the `test` dependency group because they pull external sibling
checkouts. Only `tests/test_tasks_and_providers.py` needs them; it errors with
`ModuleNotFoundError: No module named 'demo_09_15_fake_user'`. Everything else is green.

For this work, run the suite with that one file deselected:
```
uv run pytest tests/ --ignore=tests/test_tasks_and_providers.py   # 116 passed
```
Directly relevant existing tests (all in the green set): `test_entities.py` (locator/entity
storage), `test_fxtrconfig.py`, `test_runset_reload_regression.py` + `test_plan_in_session.py`
(session manager / active run manager / cache), `test_materialization_record_logging.py`. New LMDB
tests live in `tests/` and run within the same deselected set.

Caveat: `test_tasks_and_providers.py` is the one test that exercises the **end-to-end materialization
write path** (`MaterializationWorkerManager` -> `save_entity`), which is exactly the path the format
switch flows through. Since it's unavailable, the new round-trip / parity / process-pool tests must
cover the locator write path directly to compensate.

## Risks / watch items

- **map_size ceiling** in live sessions: clear `MapFullError` message; 512 GB default; bump-on-reopen
  is the escape hatch.
- **`MDB_APPEND` validity** depends on globally sorted keys; the per-shard sort + sorted shard order
  must actually produce increasing `bytes(cid)`. Verify base32 path order matches raw-byte key order
  (it may not — base32 lexical order ≠ raw byte order). **If it doesn't, sort by `bytes(cid)`
  globally per shard and process shards in `bytes(cid)` order, or drop `append` and use plain
  `putmulti`** (still fast). Decide during implementation with a quick check.
- **Multi-process writers** (SessionManager + a separate BackendRunner process on the same store):
  LMDB supports it (single-writer lock arbitrated via `lock.mdb`); keeping `writemap=False` in both
  live processes avoids the riskier writemap multiproc path.

## Implementation steps

1. [x] `WritableLocator` ABC + lift `save_entity`; make `ClosedBinaryCBOROnDiskLocator` subclass it.
2. [x] `LMDBEntityLocator` (process-wide shared env, fork-safe, single-txn `save_entity`).
3. [x] Add `lmdb` to root `pyproject.toml` deps.
4. [x] `DataConfig` fields + `get_active_entity_store_lmdb` helper.
5. [x] `get_active_data_locator` factory; route `SessionManager.create`, `BackendRunner`,
   `browse_entities.py` through it; retype the three write-path annotations.
6. [x] `scripts/migrate_entities_to_lmdb.py`.
7. [x] Tests (round-trip, parity, pickle/process-pool, fork, factory, migration) —
   `tests/test_lmdb_entity_store.py`, all green.

## Implementation notes (what changed vs. the design above)

- **Env management is process-wide, not per-instance.** Empirically, LMDB raises "already open in
  this process" if the same path is opened twice in one process — which `get_active_data_locator`
  would trigger (it hands out a fresh locator per call). So the env lives in a module-level
  `_LMDB_ENV_CACHE` keyed by resolved path, shared by all locators for that path. The instance holds
  no env handle, so it pickles trivially (no custom `__getstate__` needed — simpler than planned).
- **Fork is handled by `os.register_at_fork(after_in_child=...)`** which closes + clears inherited
  envs in the child. Confirmed by probe: a forked child cannot reopen a path the parent already
  opened *unless* the inherited handle is closed first. This matters for the run-graph cache build's
  `ProcessPoolExecutor` under the default `fork` start method (Linux). Covered by
  `test_process_pool_read_after_fork`. Side effect: `map_size` / `writemap` are only honored on the
  first open of a path within a process; later locators for the same path reuse that env.
- **Migrator copies raw `.cbor` bytes as LMDB values** (no decode/encode at all) and derives keys
  from paths — faster than the plan's "read bytes" framing implied.
- **base32 ordering gotcha (confirmed):** the CID base32 alphabet (`a-z2-7`) is NOT ASCII-ordered, so
  the migrator sorts shards and within-shard keys by base32 *symbol value* (== raw CID byte order),
  not by string. Verified empirically before relying on it for `MDB_APPEND`.
- **Resumability + batched commits.** Shards are accumulated into a transaction that commits once it
  holds >= `--commit-batch-size` entities (default 100) — sparse stores have shards of just 2-5
  entities, so per-shard txns were wasteful. Flushing only at shard boundaries keeps each shard
  wholly inside one txn, so resume stays at shard granularity with the same `<dest>.progress.json`
  sidecar format. The sidecar is now only an optimization: `putmulti(append=True)` silently skips
  already-present keys (returns `added=0`, confirmed by probe — it does NOT raise), so if a run
  crashed after a commit but before recording it, the re-read batch self-heals via a fallback
  `putmulti(append=False, overwrite=True)` (idempotent; content-addressed). Resume is therefore
  correct regardless of sidecar staleness — covered by `test_migration_resume_with_stale_sidecar`.
- **Map file is sparse:** verified the 512 GiB `data.mdb` uses ~176 KiB actual disk after a tiny
  migration, so the large default map_size is safe on APFS.
- **Test caveat unchanged:** the end-to-end materialization write path
  (`test_tasks_and_providers.py`) still can't run here (demo packages), so the write path through the
  factory is covered by the locator-level + factory tests rather than a full session run.
