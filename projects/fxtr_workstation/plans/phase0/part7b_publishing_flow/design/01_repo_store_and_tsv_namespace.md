# Piece: the repo entity store + the tsv namespace (TSV pins + by-id store + sqlite backing)

> Status: provisional first pass (2026-06-12); **updated 2026-06-14 to the
> post-stage-14 state** (the prerequisite refactor has landed). Line numbers
> approximate.
>
> **What stage 14 did (now in the code):** the by-id accumulator formerly known
> as the git store's *index namespace* is decoupled into a standalone **repo
> entity store** (`fxtr2.data.repo_storage.RepoEntityStore`, a `DeepFetchable`).
> The git-namespace backend is renamed `GitNamespaceBackend` →
> **`TsvNamespaceBackend`** (module `fxtr2.data.git_store` → `fxtr2.data.tsv_namespace`)
> and is now a *view* = repo entity store + worktree namespace + a committed
> TSV-root marker. The marker is renamed `fxtr_store.toml` → `fxtr_namespace.toml`
> ("store" now = the repo entity store; the marker defines a *namespace*). The
> internal `.fxtr/fxtr_store_repo_config.toml` keeps its name; the on-disk
> `index:{uuid}` id and `repo_backing_storage.sqlite` are unchanged (no data
> migration). The CLI is now `fxtr-tsv-ns`.

The sketch wants "the pins for a fxtr namespace, saved into a subdir of the
flake dir" and a serving script exposing "the set of all entities in the
'index namespace' (the backing blob store with all the entities pinned by
ID)". That blob store is now a first-class concept — **the repo entity
store** — and this doc describes both it and the tsv-namespace view over it as
they exist today.

Authoritative docs: `v2/fxtr2/data/tsv_namespace/LOCAL_CODE_OVERVIEW.md` and
the `fxtr2.data.repo_storage` / `fxtr2.data.repo_storage_config` module
docstrings.

## 1. On-disk format

A tsv-namespace-enabled repo looks like:

```
my-project/
├── fxtr_namespace.toml             # namespace marker: reserved_prefix (default "index"), name, description
├── fxtr_namespace.local.toml       # optional, gitignored, per-worktree overrides
├── index.fxtr.tsv                  # pins whose keys have no "prefix:" part (reserved-prefix TSV)
├── data/models.fxtr.tsv            # pins keyed "data/models:<key>"
├── fxtr_namespace_indexes.tsv      # indexer registrations (optional)
└── .fxtr/                          # sibling of the VCS root, NOT git-tracked
    ├── fxtr_store_repo_config.toml   # index_namespace_id, sqlite_store_path (name UNCHANGED)
    └── repo_backing_storage.sqlite   # the actual entity bytes (the repo entity store)
```

- **TSV files** (`tsv_namespace/tsv_file.py`): two tab-separated columns
  `key \t fxtr_id_string`, no header, keys sorted lexicographically, no
  tabs/newlines/colons in keys, no duplicates. **These are the only
  git-tracked artifact carrying pin state** — they're trivially parseable by
  non-Python code (the nix/TS sides of part 7b).
- **Key ↔ file mapping** (`tsv_namespace/key_utils.py`): a pin key `a/b:k`
  lives in `a/b.fxtr.tsv` under key `k`; a colon-less key lives in
  `<reserved_prefix>.fxtr.tsv` under itself; >1 colon is an error.
- **`fxtr_namespace_indexes.tsv`** (`tsv_namespace/index_file.py`): one
  registration per line, `indexer_fxtr_id \t json_encoded_key_prefix`.
- **Repo config** (`fxtr2.data.repo_storage_config`):
  `.fxtr/fxtr_store_repo_config.toml` holds `index_namespace_id` (on-disk key
  name kept; format `index:{uuid}`; conceptual/code name now
  `store_namespace_id`) + optional `sqlite_store_path`.

## 2. The repo entity store (`fxtr2.data.repo_storage.RepoEntityStore`)

The decoupled, first-class by-id store. Backed by the same
`SQLiteNamespaceBackend` over the `index:{uuid}` namespace in
`.fxtr/repo_backing_storage.sqlite` the index namespace always used — **no pins,
no keys, no TSVs**, just `FxtrID → bytes(+closure)`. It is a `DeepFetchable[B]`
that owns (or borrows) a sqlite session and hands out clonable leases. This is
the artifact the runner-manager rehosts and resolves remote pointers against.

Surface (verified against `repo_storage.py`):

- `RepoEntityStore.open(repo_path, *, create=False, read_only=False)` — async
  CM; discovers `.fxtr/` (via `repo_storage_config.load_repo_store_config`),
  opens its **own** `SQLiteFxtrSession`, opens the `index:{uuid}` namespace.
  - `create=True` mints `.fxtr/` + `fxtr_store_repo_config.toml` if absent.
  - `read_only=True` opens a read-only session (store must already exist).
  - **`create=True` and `read_only=True` are mutually exclusive** (raises
    `ValueError`): read-only sessions skip schema init. "Create-if-absent then
    read-only" is therefore a **two-pass** dance — a writable `create=True`
    pass, then reopen `read_only=True` — *or* just open `create=True` writable
    and never write. (This is a concrete wrinkle for part 1 §3 — see that doc.)
- `from_session(session, store_ns)` — wrap an already-open session (does NOT
  own it). This is how `TsvNamespaceBackend` exposes its existing index
  namespace as `tsv_ns.store` without opening a second session.
- `try_lookup(id) -> ReadableArena | None` / `read(id)` (alias) — by-id read
  (the `DeepFetchable` impl; resolves any id in a tracked closure, not only
  pinned roots).
- `write_from_arena(entity_id, source, src_lease)` — by-id write, no pin.
- `get_lease() -> Lease[B]` — a fresh **clone** of the session's root lease.
  (`_root_lease` is private — always go through `get_lease()`.) `.token` is the
  session token. The two downstream consumption shapes both use this:
  - `LayeredFileStoreReader(remote=(store, store.get_lease()))` — the
    `remote=` param takes the `(DeepFetchable, Lease)` pair **directly**
    (`layered_file_store/reader.py:59`).
  - `scope.register_deep_fetch_source(store, IndependentLocalBridge(lease=store.get_lease()))`
    — takes a `LocalBridge`, **not** a bare lease
    (`entity_ref_scope.py:4813`); the `Independent` (case-1) variant is for
    when the store session outlives the consuming scope.

## 3. The tsv namespace view (`TsvNamespaceBackend`, `fxtr2.data.tsv_namespace`)

A `TsvNamespaceBackend` = a repo entity store + a worktree namespace + the
committed TSV-root marker. Both sqlite namespaces live in the same backing
store (`.fxtr/repo_backing_storage.sqlite`):

- **Worktree namespace** (`worktree:{abs_path}`, per checkout) mirrors the
  current TSV state — pins by their user-facing keys.
- **Index namespace** (`index:{uuid}`, shared across worktrees) = the **repo
  entity store**, accessed internally as `_index_ns` and wrapped as `_store`
  (a `RepoEntityStore` via `from_session`, sharing the session). It
  accumulates **every entity ever pinned, keyed by its own id-string**.
  `pin_entity_from_arena` writes the entity (+ closure) into the store first,
  then pins into the worktree; `unpin_entity` removes only the worktree pin —
  the store keeps the data so old git revisions stay loadable.

The store is exposed as **`tsv_ns.store`** (a `RepoEntityStore`) so a process
that already holds a `TsvNamespaceBackend` can hand the by-id store to the
runner without re-discovery (the runner-manager's stated default is still to
discover its own — see part 1 §3).

This index/repo-store is exactly the sketch's "backing blob store with all the
entities pinned by ID" — the serving script's "(b)" source. Caveat: it's an
append-only *local* artifact (gitignored); a fresh clone has TSVs but an empty
store until entities are imported/pulled. How a published flake's consumers
get blob data is therefore the remote-store question, not a git question.

### Sync machinery (TSV ⇄ sqlite)

`tsv_namespace/sync.py` + the dirty-mark system (`dirty.py`):

- `sync_tsv_to_sqlite`: per TSV entry, verify the entity exists in the **index
  namespace (repo store)**, then pin in the worktree namespace. (So a TSV pin
  is only loadable if its blobs are present locally.)
- `dump_sqlite_to_tsv`: flush worktree pins back to TSVs (with backup files on
  concurrent-edit detection).
- Dirty marks batch writes; sqlite is source of truth while dirty, TSV when
  clean. After `git checkout`, opening the backend full-syncs changed TSVs in.

### API surface (`tsv_namespace/tsv_namespace.py`)

- `TsvNamespaceBackend.open(path, auto_flush_seconds=…, …)` async CM /
  `TsvNamespaceBackend.init(path, …)`.
- `pin_entity_from_arena(key, entity_id, source, src_lease, overwrite)` /
  `unpin_entity(key)` / `retrieve_pinned(key) -> (FxtrID, arena)` /
  `list_pins(prefix)`.
- **`try_lookup(node_id)` (DeepFetchable)** — by-id lookup served from the repo
  store. (For the runner's purposes, prefer the standalone `RepoEntityStore`;
  this method is the same data via the full backend.)
- Indexing: `register_indexer` / `unregister_indexer` / `iter_matches`.
- Utility: `flush()`, `sync_from_git()`, `full_sync_state_to_git()`,
  `export_archive` / `import_archive_into_backing_store`.
- `store` property → the backing `RepoEntityStore`.
- User-facing wrapper: `TsvFxtrNamespace` in `data/fxtr_context_api.py`.

CLI (`v2/fxtr2/cli/tsv_ns_cli.py`): `fxtr-tsv-ns
{init, sync-from-tsv, flush-to-tsv, export, import, list, status}`.

### Config split

`fxtr2.data.repo_storage_config` is the **lower** layer (store-only,
namespace-marker-free, importable without a circular dep): `RepoStoreConfig`,
`find_vcs_root` / `find_fxtr_dir`, `.fxtr/` repo-config parsing,
`load_repo_store_config`, `ensure_repo_config_initialized`, `FxtrConfigError`.
`tsv_namespace/config.py` is the namespace-marker layer (`TsvNamespaceConfig`,
`fxtr_namespace.toml` parsing, `init_namespace` / `namespace_exists`); it
**composes + re-exports** the lower-layer names for back-compat. The demo's
sqlite/repo placement overrides (`sqlite_store_path`, `repo_config_path`) live
here (DECIDED-1).

## 4. Implications for part 7b

- The "pins subdir of the flake dir" can literally be a tsv namespace dir (an
  `fxtr_namespace.toml` + TSVs) committed inside the publishing flake — or just
  bare TSV files following the same format. Whether it's a *full* namespace
  (with its own `.fxtr/` repo store) or a pin-list-only convention is a key
  design choice (see `discussion_topics.md`); DECIDED-1 picks the real
  namespace.
- TSVs carry **ids only** — no blob bytes, no precomp/provenance info. A
  nix step rewriting TSVs into a layer can produce pins + remote-pointers,
  but precomp entries for provenance-tagged pinned ids need the actual
  node data from somewhere: the remote (repo store, rehosted) at build time.
  _Daniel note:_ I think we should focus on these coming from the remote.
- The sketch's "sqlite store that just tracks blobs" matches the repo entity
  store exactly: the publishing repo's `.fxtr/repo_backing_storage.sqlite`,
  now reachable standalone via `RepoEntityStore.open(...)` — which is precisely
  why the runner-manager (part 1) can rehost/resolve against it without
  standing up the full `TsvNamespaceBackend`.
