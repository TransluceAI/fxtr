# Stage 14 — repo entity store refactor (implementation plan)

> Spec for the refactor scoped in `overview.md`. Decouples the index namespace
> into a standalone `RepoEntityStore`, makes `GitNamespaceBackend` a view over
> it, and hard-renames the committed marker `fxtr_store.toml` →
> `fxtr_namespace.toml`. Working notes (session/lease mechanics, semantics
> choices, full sweep file list) live in `plan_scratch.md`.

## Goal & shape

`fxtr_nix_runner` (future flake manager) must read entity bytes by `FxtrID` —
to rehost them for build-time FOD fetches and resolve remote-pointer entities —
**without** instantiating a `GitNamespaceBackend` (worktree sync, dirty marks,
TSV discovery). So we lift the by-id "index namespace" out as a first-class
**repo entity store** and make the git namespace one view on top.

Two consumption shapes are fixed by existing fxtr2 contracts (confirmed by
reading them — no spike):
- `LayeredFileStoreReader(remote=(DeepFetchable, Lease))` (reader.py:59)
- `scope.register_deep_fetch_source(source, LocalBridge)` (entity_ref_scope.py)

The store therefore exposes a `(DeepFetchable, Lease)` pair; callers pick the
bridge.

## Design decisions (resolved)

- **Session ownership.** `GitNamespaceBackend` keeps owning its single
  `SQLiteFxtrSession` and builds the embedded store *over that session*
  (internal constructor), exposing `git_ns.store`. `RepoEntityStore.open()` is
  a **separate standalone path** that opens its own session (runner case). One
  session per process in the git-ns case → the shared-lease pin path is
  unchanged. (Why this matters: the existing pin path copies index→worktree
  using one shared session lease; see `plan_scratch.md`.)
- **By-id read = `try_lookup` semantics** (resolves any entity in a tracked
  closure, not only individually-pinned roots) — the runner needs closure
  children, not just roots. `read` and `try_lookup` are the same method.
- **On-disk compat.** No format change, no migration: same
  `index:{uuid}` namespace, same `.fxtr/repo_backing_storage.sqlite`. The
  on-disk key `index_namespace_id` stays; only the *conceptual* name changes
  (`store_namespace_id`) in code.
- **`.fxtr/fxtr_store_repo_config.toml` is NOT renamed** (it configures the
  store). Only the committed *namespace* marker is renamed.
- **CLI** `fxtr-git-store` → `fxtr-git-ns`, module file `git_store_cli.py` →
  `git_ns_cli.py` (single importer: `test_cli.py`).

## Public API (new module `fxtr2.data.repo_storage`)

```
RepoEntityStore[B]                       # by-id store, branded by its session B
  @classmethod open(repo_path, *, create=False, read_only=False) -> async CM[RepoEntityStore]
       # standalone: discovers .fxtr/, opens own SQLiteFxtrSession, opens the
       # index:{uuid} namespace. create=True mints .fxtr/ + repo config if absent.
       # read_only=True opens the session read-only. create + read_only are
       # mutually exclusive (raise) — read-only sessions skip schema init, so a
       # store must already exist; the runner does a writable create pass first,
       # then reopens read_only.
  @classmethod from_session(session, repo_store_config) -> RepoEntityStore
       # internal: wrap an already-open session (used by GitNamespaceBackend);
       # does NOT own session lifetime.
  read(id) -> ReadableArena | None         # == try_lookup
  try_lookup(id) -> ReadableArena | None   # DeepFetchable impl
  write_from_arena(id, source, src_lease)  # by-id write (pin by id_string), no pin/key/TSV
  get_lease() -> Lease[B]                   # clone of the (private) session root lease
  token       (property)                    # session token
  # _root_lease is private — callers go through get_lease() (clones), never the raw lease.
```

- Subclasses `DeepFetchable[B]` (abstract_arena.py:596). `get_lease`/`token`
  mirror `SQLiteNamespaceBackend`; the raw session lease is held privately as
  `_root_lease` (not exposed) so callers can't muck with it — `get_lease()`
  hands out clones. Read-only sessions use the session's existing
  `_readonly_lifecycle_handler` path (sqlite_namespace.py ~403).
- Internally backed by the same `SQLiteNamespaceBackend` over the
  `index:{uuid}` namespace the index namespace uses today — on-disk-compatible.

### Config helpers (extracted from `git_store/config.py`)

```
RepoStoreConfig                  # store-only subset, loadable standalone
  fxtr_dir, sqlite_store_path, store_namespace_id   # value still "index:{uuid}"
load_repo_store_config(repo_path, *, create=False) -> RepoStoreConfig
       # discovery (find_vcs_root/find_fxtr_dir, already exist) + optional
       # .fxtr/ init; independent of load_config (no fxtr_namespace.toml needed)
GitStoreConfig                   # composes RepoStoreConfig; keeps reserved_prefix,
                                 # worktree_namespace_id, store_root(_str), name, desc
```

`init_store`'s `.fxtr/`-init logic is factored so both `init_store` and
`RepoEntityStore.open(create=True)` share it. `GitStoreConfig` forwards to the
nested `RepoStoreConfig` (properties) to keep the call-site diff small.

## `GitNamespaceBackend` re-plumb (behavior unchanged)

- `open()` still opens the session + worktree namespace; additionally builds
  `RepoEntityStore.from_session(session, config)` and holds it as `self._store`.
- Replace direct `self._index_ns` use with the store:
  - `pin_entity_from_arena`: `self._store.write_from_arena(...)` then retrieve
    via the store for the worktree pin (same two-step as today).
  - `try_lookup` / retrieve / sync: resolve blobs through `self._store`.
  - `register_indexer` / `_import_*`: route index-namespace writes through the
    store.
- Expose `git_ns.store` (property) so callers holding a git namespace can hand
  its store to the runner without re-discovering.
- Public behavior is byte-identical; `test_git_store/` passes as-is (modulo the
  rename in step 4).

## The rename (own isolated commit)

`fxtr_store.toml` → `fxtr_namespace.toml`, `.local.toml` likewise. **No
back-compat read of old names** (all consumers in-repo). Mechanical, isolated
commit. Full file list in `plan_scratch.md`.
Sweep active code/tests, the
4 demo stores (rename files on disk + `.gitignore`s + `run_demo.py`), the CLI,
and the **part7b design docs** (`design/01`, `design/06`, `discussion_topics`,
`overview`, `part6`). **Leave historical plans as-is** (`v2/notes/…`,
`v2/plans/phase2/*`, `v2/plans/phase3/stage1/*`).

## Implementation steps

- [x] **1. Config extraction.** Add `RepoStoreConfig` + `load_repo_store_config`
      + factor `.fxtr/` init out of `init_store` (`ensure_repo_config_initialized`)
      + shared `_resolve_store_namespace_id`/`_resolve_sqlite_store_path`; make
      `GitStoreConfig` compose `RepoStoreConfig` (forward `fxtr_dir`/
      `sqlite_store_path`/`index_namespace_id` via properties). No behavior
      change; full `test_git_store/` green (325). `LOCAL_CODE_OVERVIEW.md`
      config section updated; new symbols exported from `git_store/__init__.py`.
- [x] **2. `RepoEntityStore`.** New `fxtr2/data/repo_storage.py`. Implements
      `open` (with `create`/`read_only`; create+read_only guarded),
      `from_session`, `read`/`try_lookup`, `write_from_arena`, `get_lease`
      (private `_root_lease`), `token`, `DeepFetchable`. Tests in
      `tests/test_repo_storage.py` (9, all green): open-existing,
      create-if-absent, open-missing-raises, create+read_only guard,
      read/write/try_lookup round-trip, idempotent write, missing→None,
      read-only reopen-and-read, and `DeepFetchable` adoption into a scope via
      `register_deep_fetch_source(store, IndependentLocalBridge(...))`.
      `v2/CODE_OVERVIEW.md` updated. (No `LayeredFileStoreReader` smoke test —
      adoption + by-id round-trip cover the contract; can add in part 1.)
- [x] **3. Re-plumb `GitNamespaceBackend`.** Holds `RepoEntityStore`
      (`_store`, via `from_session` over the existing session+index ns); by-id
      entity-bytes writes (`pin_entity_from_arena`, `register_indexer`) go
      through `self._store.write_from_arena` + `try_lookup`; exposes `.store`.
      `_index_ns` kept as the raw handle for sync/indexing/import machinery.
      Full `test_git_store/` + `test_repo_storage.py` green (334, pre-rename).
      No circular import (git_namespace → repo_storage → git_store.config
      submodule, which has no back-edge). `LOCAL_CODE_OVERVIEW.md` updated
      (store-vs-namespace framing, `.store`, stale `new_empty_arena` removed).
- [x] **4. Isolated rename commit.** `fxtr_store.toml` → `fxtr_namespace.toml`
      (+`.local.toml`) across active code/tests/demos/CLI/part7b-docs. Renamed 4
      demo markers on disk + their `.gitignore`s + `run_demo.py`; CLI module
      `git_store_cli.py` → `git_ns_cli.py`, command → `fxtr-git-ns`,
      `pyproject.toml` + `test_cli.py` import updated.
      (Follow-up commit renamed the
      constants `STORE_CONFIG_FILENAME` → `NAMESPACE_CONFIG_FILENAME` and
      `LOCAL_CONFIG_FILENAME` → `NAMESPACE_LOCAL_CONFIG_FILENAME` to match the
      new framing — config + test_config + behaviors_hub test imports.)
      part7b design-doc
      sweep preserved the "X → Y" rename-description lines. Verified: v2
      git_store + repo_storage (334)
      green; `fxtr-git-ns --help` resolves. **Left as-is:** committed
      `.fxtr.tsv.dirty/DO_NOT_COMMIT…md` demo sentinels (frozen generated
      artifacts; the generating source `dirty.py` was updated).
- [x] **5. Docs.** `git_store/LOCAL_CODE_OVERVIEW.md` (store-vs-namespace
      framing, `.store`, `repo_storage` module, stale `new_empty_arena`
      removed) + `v2/CODE_OVERVIEW.md` (new module entry, CLI line). No separate
      `repo_storage/LOCAL_CODE_OVERVIEW.md` — it's a single file, covered by the
      CODE_OVERVIEW entry + the module docstring.

## Non-goals / invariants preserved
- No on-disk format change / migration; `index:{uuid}` id + backing path
  unchanged; on-disk `index_namespace_id` key unchanged.
- No change to pin/TSV/dirty/sync semantics — pure extraction + re-plumb +
  rename.
- Store stays local + gitignored; how a fresh clone populates it is orthogonal.
- `reserved_prefix` default inconsistency is pre-existing; not touched here.
