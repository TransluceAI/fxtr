# Stage 14 — repo entity store refactor (decouple the index namespace)

> Status: DESIGN (2026-06-13). Prerequisite for the workstation publishing
> flow (`projects/fxtr_workstation/plans/phase0/part7b_publishing_flow/`),
> which is the motivation for this change — but the change itself is a
> fxtr2-level refactor of `v2/fxtr2/data/git_store/` and stands on its own.

## Why

Today the **index namespace** — the by-id accumulator that holds every
entity ever pinned in a repo, keyed by its own `FxtrID` — is an internal
implementation detail of `GitNamespaceBackend`. It lives in
`.fxtr/repo_backing_storage.sqlite` under the namespace id `index:{uuid}`,
and the only way to read or write it is *through* the git namespace (pin a
key, retrieve a key, or `try_lookup` by id on the backend object).

Workstation part 7b needs a **second consumer** of exactly this store that has nothing
to do with TSV pins: `fxtr_nix_runner` (promoted to a fxtr2-aware "flake
manager") must, given a repo, read entity bytes by id to (a) rehost them
over HTTP for build-time FOD fetches and (b) resolve remote-pointer entities
when opening nix-built layers. It should not have to instantiate a full
`GitNamespaceBackend` (with its worktree-sync machinery, dirty marks, TSV
discovery) just to read blobs by id.

So we pull the index namespace out as a first-class, standalone concept —
**the repo entity store** — and make `GitNamespaceBackend` one view on top
of it rather than its owner.

## The reframing

- **Repo entity store** ("the store"): repo-associated, by-id entity
  storage. One per repo (the `.fxtr/` dir, sibling of the VCS root). It is
  *the artifact formerly known as the index namespace*. Read/write entities
  by `FxtrID`; **no pins, no keys, no TSVs**. Gitignored, local, append-y
  (mirrors the current index-namespace semantics: entities accumulate; the
  git namespace never deletes from it so old revs stay loadable).
- **Git namespace** (`GitNamespaceBackend`): a *view* = a repo entity store
  + a worktree namespace (the per-checkout pin mirror) + a committed TSV-root
  marker. The pin/sync/dirty machinery is unchanged; it just gets its by-id
  storage from the store object instead of owning the sqlite namespace
  directly.

Terminology consequence: **"store" now specifically means the repo entity
store.** The user-facing pinned view is a "namespace". This is why the
committed marker file is renamed (below) from `fxtr_store.toml` to
`fxtr_namespace.toml` — that file defines a *namespace* view, not the store.
The internal config file `.fxtr/fxtr_store_repo_config.toml` **keeps** its
name: it configures the (internal, not-user-facing) store.

## What changes

### 1. New module: `fxtr2.data.repo_storage`

A standalone store object + the reusable discovery/config helpers.

```
RepoEntityStore[B]                   # the by-id store object (branded by its session)
  .open(repo_path, *, create=False)  # async CM; discovers .fxtr/, opens sqlite ns
  .read(id) -> ReadableArena | None  # by-id read (the DeepFetchable shape)
  .write_from_arena(id, source, src_lease)   # by-id write, no pin
  .try_lookup(id) -> ReadableArena | None     # DeepFetchable impl (== read)
  .new_empty_arena() -> CopyTargetArena
  .get_lease() / .root_lease         # session-lifetime lease (clonable)
```

- Backed by the same `SQLiteNamespaceBackend` over
  `.fxtr/repo_backing_storage.sqlite` at namespace id `index:{uuid}` that the
  index namespace uses today — i.e. **on-disk-compatible**, no data
  migration.
- `create=True` initializes an empty store (mints the `index:{uuid}`, writes
  `.fxtr/fxtr_store_repo_config.toml`) if absent — this is what lets the nix
  runner "create-if-absent then open read-only" (it always passes
  `create=True` so a fresh clone with no `.fxtr/` still works).
- Subclasses `DeepFetchable[B]` (`abstract_arena.py:596`) and owns a sqlite
  **session**, exposing a clonable lease — exactly the shape
  `GitNamespaceBackend`/`GitFxtrNamespace` already present (this refactor lifts
  that pattern out). The two downstream consumption shapes are **fixed by the
  existing fxtr2 contracts**, confirmed by reading them (no spike):
  - `LayeredFileStoreReader(remote=(DeepFetchable, Lease))` (`reader.py:59`)
    takes the store + a lease clone **directly**.
  - `scope.register_deep_fetch_source(source, bridge)`
    (`entity_ref_scope.py:4813`) takes a **`LocalBridge`**, not a bare lease;
    consumers whose scope is outlived by the store session use an
    `IndependentLocalBridge` (case 1; the pattern `HttpFxtrReadNamespace`
    uses). The runner manager (part 1) exposes the store as a
    `(DeepFetchable, Lease)` pair and the part-5 serving script picks the
    bridge.

**Config/discovery helpers, made reusable** (extracted from `config.py` so
they don't require a `GitStoreConfig`/`GitNamespaceBackend`):

- `find_vcs_root(path)` / `find_fxtr_dir(...)` — already exist; expose them
  as the store-discovery entry points.
- A small `RepoStoreConfig` (subset of today's `GitStoreConfig`:
  `fxtr_dir`, `sqlite_store_path`, `index_namespace_id` → rename the field
  conceptually to `store_namespace_id`, value format unchanged `index:{uuid}`)
  loadable on its own from `.fxtr/fxtr_store_repo_config.toml`.
- `GitStoreConfig` keeps the rest (`reserved_prefix`, `worktree_namespace_id`,
  TSV-root marker path) and composes the `RepoStoreConfig` in.

### 2. `GitNamespaceBackend` becomes a view over the store

- Internally hold a `RepoEntityStore` (or its sqlite namespace) instead of
  opening the index namespace directly. `pin_entity_from_arena` writes to the
  store first (today's "copy to index namespace first"), then pins in the
  worktree namespace. `try_lookup`/retrieve resolve blobs through the store.
- Public `GitNamespaceBackend` behavior is **unchanged** — this is an
  internal re-plumb. Existing tests should pass as-is (modulo the rename
  below).
- Optionally expose `git_ns.store` so callers that already hold a git
  namespace can hand its store to the runner without re-discovering.

### 3. Hard rename of the committed marker

`fxtr_store.toml` → `fxtr_namespace.toml`, and `fxtr_store.local.toml` →
`fxtr_namespace.local.toml`. **No back-compat reading of the old names** —
all consumers are in-repo. Land this as its **own isolated commit** so the
diff is obviously mechanical. In-repo occurrences to sweep (from a grep at
plan time): `config.py`, `key_utils.py`, `git_namespace.py`,
`cli/git_store_cli.py`, `tests/test_git_store/*`, the
`v2/examples/session_manager_porting/` demo stores + `.gitignore`s, and the
part7b design docs (`design/01`, `design/06`, `part6`) + this stage's docs.

`.fxtr/fxtr_store_repo_config.toml` is **NOT** renamed (it configures the
store, and "store" is the right word for it).

### 4. CLI

`fxtr-git-store` is renamed to `fxtr-git-ns` and becomes the git-namespace CLI
(it operates on the namespace view). If a store-only CLI proves useful later
(e.g. `fxtr-repo-store read/write/list-ids`) it can be added then — out of scope here.

## Non-goals / preserved invariants

- No on-disk format change, no data migration: the sqlite layout, the
  `index:{uuid}` id, and the backing-store path are all unchanged.
- No change to pin/TSV/dirty/sync semantics — purely an extraction +
  re-plumb + rename.
- The store stays **local + gitignored**. How a fresh clone *populates* it
  (remote pull, import-archive) is unchanged and orthogonal.

## Implementation steps

- [ ] Extract `RepoStoreConfig` + reusable discovery helpers from `config.py`
      (no behavior change; `GitStoreConfig` composes it).
- [ ] Add `fxtr2.data.repo_storage.RepoEntityStore` (open/create, by-id
      read/write, `DeepFetchable`) over the existing sqlite namespace; unit
      tests (open-existing, create-if-absent, read/write/try_lookup round
      trip, DeepFetchable adoption into a scope).
- [ ] Re-plumb `GitNamespaceBackend` to hold a `RepoEntityStore` internally;
      expose `.store`. Confirm the full `test_git_store/` suite passes.
- [ ] **Isolated commit:** hard-rename `fxtr_store.toml` →
      `fxtr_namespace.toml` (+ `.local.toml`) across the repo.
- [ ] Update `v2/fxtr2/data/git_store/LOCAL_CODE_OVERVIEW.md` (the store vs.
      namespace framing; the new `repo_storage` module) and a new
      `repo_storage/LOCAL_CODE_OVERVIEW.md` if it warrants its own dir, and
      `v2/CODE_OVERVIEW.md`.

## Open questions (resolve at plan-detail time)

- **Module home/name:** `fxtr2.data.repo_storage` as a sibling of
  `git_store`, vs. a `repo_storage.py` inside `git_store/`. Leaning sibling
  module (it's no longer git-specific). Confirm the public name
  (`RepoEntityStore`).
  _Daniel note:_ Both sound good!
- **Session lifetime / co-existence.** Resolved for ws part 7b: the store owns
  its own session, opened standalone (no `GitNamespaceBackend` in the runner
  process), with session lifetime = the manager's lifetime. The sqlite store
  supports concurrent sessions, so we don't need to worry about concurrent
  access with git namespace backends.
- Whether `index_namespace_id` is renamed on disk to `store_namespace_id`
  (a second mechanical rename inside `.fxtr/fxtr_store_repo_config.toml`) or
  left as-is for zero migration. Leaning: leave the on-disk key, rename only
  in code/conceptually — call this out.
  _Daniel note:_ Reasonable to keep as-is for now.
