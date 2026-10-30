# Stage 14 scratch — session/lease mechanics & semantics working-through

Scratch notes behind `plan.md`. Not a spec; captures the reasoning so the
plan can stay terse.

## The session-sharing constraint (resolved with Daniel)

Today `GitNamespaceBackend.open` opens **one** `SQLiteFxtrSession` and creates
**both** namespaces from it:

```python
async with SQLiteFxtrSession.open(sqlite_path) as session:
    index_ns    = await session.create_or_retrieve_user_namespace(config.index_namespace_id)
    worktree_ns = await session.create_or_retrieve_user_namespace(config.worktree_namespace_id)
```

The pin path copies into `index_ns`, retrieves an arena from it, then pins into
`worktree_ns` passing `self._session.root_lease.clone()` (git_namespace.py
~445). That cross-arena copy uses the **shared session lease/brand `B`** — so
the two namespaces *must* live on one session. This is the load-bearing
constraint for the refactor.

### Decision: two construction paths for `RepoEntityStore`

- **`RepoEntityStore.open(repo_path, *, create=False)`** — async CM, the
  **standalone** path (runner / part 1). Discovers `.fxtr/`, opens its *own*
  `SQLiteFxtrSession`, creates/opens the `index:{uuid}` namespace, owns session
  lifetime. `create=True` mints `.fxtr/` + `fxtr_store_repo_config.toml` if
  absent (fresh-clone case).
- **`RepoEntityStore.from_session(session, config)`** (internal/alt
  constructor) — wraps an **already-open** session + its index namespace; does
  *not* own the session lifetime. This is what `GitNamespaceBackend` uses:
  it keeps owning the session (as today) and builds the embedded store over it,
  exposing it as `git_ns.store`.

So in the git-ns case there is still exactly **one** session per process and
the shared-lease pin path is unchanged. In the runner case the store owns its
session standalone, no `GitNamespaceBackend` involved.

Branding: `RepoEntityStore[B]` carries the session brand `B` (same pattern as
`SQLiteNamespaceBackend[B]` / `SQLiteFxtrSession[B: FreshLifetime]`). `get_lease`
/ `root_lease` clone the session root lease, exactly like
`SQLiteNamespaceBackend.get_lease()` (sqlite_namespace.py ~973).

## read / write-by-id semantics

The index namespace today *is* a `SQLiteNamespaceBackend`; "by-id storage" is
implemented as pinning keyed by `entity_id.id_string()`:

- **write**: `index_ns.pin_entity_from_arena(id.id_string(), id, source, lease, overwrite=True)`
  — individually roots each entity so a later worktree unpin can't GC it
  (this is the "index keeps data so old revs stay loadable" invariant).
- **read**: two candidate impls —
  - `retrieve_pinned(id.id_string())` → only finds individually-pinned roots.
  - `try_lookup(id)` (DeepFetchable) → finds *any* tracked entity, incl.
    closure children that were never individually pinned.

`RepoEntityStore.read(id)` / `.try_lookup(id)` use the **`try_lookup`
semantics** (resolve any id in a tracked closure). Rationale: the store's job
for the runner is "give me bytes for this id so I can rehost / resolve remote
pointers" — that must reach closure children, not just pinned roots. `read`
and `try_lookup` are the same method (plan API lists both; `try_lookup` is the
`DeepFetchable` name, `read` the friendly alias).

`write_from_arena(id, source, src_lease)` = the pin-by-id-string write above,
minus any worktree/TSV involvement.

## new_empty_arena — DROPPED (Daniel)

`git_store/LOCAL_CODE_OVERVIEW.md:588` documents a `new_empty_arena` on
`GitNamespaceBackend` that doesn't exist in `git_namespace.py` (stale doc).
It is **not** part of the `RepoEntityStore` API — Daniel confirmed it's no
longer supposed to be there. The part-1 consumer shapes only need
`(DeepFetchable, Lease)`. Also delete the stale `new_empty_arena` entry from
`git_store/LOCAL_CODE_OVERVIEW.md` during the doc pass.

## read_only + private lease (Daniel)

- `RepoEntityStore.open(..., read_only=False)` — opens the session read-only
  when set. The runner's "create-if-absent then read" → `open(create=True,
  read_only=True)`. The session already supports this via
  `_readonly_lifecycle_handler` (sqlite_namespace.py ~403); thread the flag
  through to `SQLiteFxtrSession.open`.
- The raw session lease is held **privately as `_root_lease`** — no public
  `root_lease` property. Callers use `get_lease()` (returns clones) so nobody
  mucks with the underlying lease.
- Sequencing nuance (resolved): `SQLiteFxtrSession.open(read_only=True)`
  **skips schema init** (confirmed, sqlite_namespace.py ~577). So **`create`
  and `read_only` are mutually exclusive** — `open(create=True,
  read_only=True)` raises. A read-only store must already exist; the runner
  does a writable `open(create=True)` pass first to mint `.fxtr/` +
  `index:{uuid}`, closes it, then reopens `open(read_only=True)`.

## DeepFetchable adoption shapes (fixed by existing contracts, no spike)

- `LayeredFileStoreReader(remote=(DeepFetchable, Lease))` (reader.py:59) — pass
  `(store, store.get_lease())`.
- `scope.register_deep_fetch_source(source, bridge)` — `bridge` is a
  `LocalBridge`, not a bare lease. Standalone-session consumers use
  `IndependentLocalBridge(lease=store.get_lease())` — exactly the pattern
  `GitFxtrNamespace.__asynccontextmanager__` already uses
  (fxtr_context_api.py:776) and `HttpFxtrReadNamespace`. The part-5 serving
  script picks the bridge; the store just exposes `(DeepFetchable, Lease)`.

## Config extraction (step 1)

`GitStoreConfig` (config.py:60) fields split into:

- **`RepoStoreConfig`** (new, store-only): `fxtr_dir`, `sqlite_store_path`,
  `store_namespace_id` (conceptual rename of `index_namespace_id`; **on-disk
  key stays `index_namespace_id`** per Daniel note). Loadable on its own from
  `.fxtr/fxtr_store_repo_config.toml` without needing a `store_root` /
  `fxtr_namespace.toml`.
- **`GitStoreConfig`** keeps `store_root`, `store_root_str`, `reserved_prefix`,
  `worktree_namespace_id`, `name`, `description`, and **composes** a
  `RepoStoreConfig` (so `config.index_namespace_id` etc. keep working via
  delegation/property to avoid churning every call site — decide property
  forwarding vs. field move at impl time; prefer forwarding to keep the diff
  small).
- `find_vcs_root` / `find_fxtr_dir` already exist and are reusable as-is;
  expose them as the store-discovery entry points. Add a
  `load_repo_store_config(repo_path, *, create=False)` that does the
  discovery + (optional) init of `.fxtr/` independent of `load_config`
  (which today requires a `fxtr_namespace.toml`). `init_store`'s `.fxtr/`-init
  logic (config.py ~520-532) gets factored so both `init_store` and
  `RepoEntityStore.open(create=True)` share it.

## The rename sweep (step 4) — concrete file list

`fxtr_store.toml` → `fxtr_namespace.toml`, `fxtr_store.local.toml` →
`fxtr_namespace.local.toml`. **No back-compat reading of old names.**

Active (sweep):
- `v2/fxtr2/data/git_store/config.py` (`STORE_CONFIG_FILENAME`,
  `LOCAL_CONFIG_FILENAME` constants + the gitignore template line + docstrings)
- `v2/fxtr2/data/git_store/git_namespace.py` (docstrings)
- `v2/fxtr2/data/git_store/key_utils.py:62` (error message)
- `v2/fxtr2/cli/git_store_cli.py` (→ rename file to `git_ns_cli.py`; command
  `fxtr-git-store` → `fxtr-git-ns`; update help text)
- `v2/pyproject.toml:42` entry-point
- `v2/tests/test_git_store/test_cli.py:18` import + any literal refs
- `v2/tests/test_git_store/test_config.py`, `test_git_namespace.py` literals
- 4 demo stores: rename the committed
  `v2/examples/session_manager_porting/v2_version/storage/*/fxtr_store.toml`
  files on disk + their `.gitignore` lines + `run_demo.py` refs
- part7b design docs: `design/01_git_store.md`, `design/06_proposed_flow.md`,
  `discussion_topics.md`, `overview.md`, `part6_*.md`
- this stage's docs + `git_store/LOCAL_CODE_OVERVIEW.md`, `v2/CODE_OVERVIEW.md`

Leave as-is (historical):
- `v2/notes/migrating_to_transitional_sm.md`
- `v2/plans/phase2/*`, `v2/plans/phase3/stage1/*`

NOT renamed: `.fxtr/fxtr_store_repo_config.toml` (configures the store);
on-disk `index_namespace_id` key.

## Post-impl findings (2026-06-13, after first pass)

- **Circular import (real bug, fixed — then fixed *properly*).** `repo_storage`
  importing `fxtr2.data.git_store.config` at module top forms a cycle: that
  submodule import runs `git_store/__init__`, which eagerly imports
  `git_namespace`, which imports `repo_storage` (still partially initialized) →
  `ImportError`. Masked in the test suite because something always imports
  `git_store` before any bare `import fxtr2.data.repo_storage` (priming the
  modules), but the runner's standalone `import fxtr2.data.repo_storage` would
  have failed. **First fix** (band-aid): lazy-import `load_repo_store_config`
  inside `open()`. **Proper fix** (follow-up commit): the root cause was a
  layering inversion — store-only config/discovery sitting inside the higher
  `git_store` package. Extracted it to a new lower module
  `fxtr2.data.repo_storage_config` (`RepoStoreConfig`, `find_vcs_root`/
  `find_fxtr_dir`, `.fxtr/` repo-config parsing + resolvers,
  `load_repo_store_config`, `ensure_repo_config_initialized`, `FxtrConfigError`,
  the `.fxtr`-level constants). `git_store/config.py` now keeps only the
  namespace-marker layer and *composes + re-exports* the lower names (via
  `__all__`) so every `from fxtr2.data.git_store.config import …` call site is
  unchanged. `repo_storage` imports `repo_storage_config` at top level (lazy
  import removed); dependency is now strictly
  `git_store → repo_storage → repo_storage_config`. Regression guard:
  `test_repo_storage_importable_standalone` (bare import in a fresh subprocess).
- **Deep-fetch "hang" was a test bug, not a product bug.** `scope._deep_fetch`
  returns the *consumer scope's* `MemoryArena` (branded to the consumer token).
  Loading it with a *store-session* lease (`store.get_lease()`) is a brand
  mismatch and `MemoryArena.load` **awaits forever** instead of erroring. With
  the correct `consumer.clone_authorization()` lease the round-trip works
  end-to-end. The test now does the full load-and-verify.
- **Pre-existing wart (FIXED):** the "hang" was a downstream symptom of a
  one-line lease-leak bug in `MemoryArena`. On a **wrong-brand lease** (subject ≠
  the arena's token), the precheck `_assert_lease_matches_arena` correctly raises
  `LeaseGrantMismatchError` — but used to do so **without discarding the lease it
  was handed** (the next branch, `node_id not in liveset`, already did
  `lease.discard()` first; this path just forgot). `load`/`store`/`is_tracked`
  are contracted to consume their lease, so that was a leak. When the leaked
  lease was a **session** lease it pinned the session lifetime, so the owning
  context manager's teardown drain awaited forever — the "hang" observed during
  dev (GC-timing-dependent, hence it looked racy). **Fix:**
  `_assert_lease_matches_arena` now `lease.discard()`s before raising on a
  subject mismatch (covers both its callers: `_precheck_lease_for_arena` and
  `is_tracked`). Regression test:
  `tests/test_memory_arena.py::test_load_wrong_brand_lease_is_consumed` (now a
  normal green test; pure `MemoryArena`, no git/RepoEntityStore/leak-counter
  machinery). Not introduced by stage 14, but easy and safe to fix here.

## Open / to-confirm during impl
- `new_empty_arena` backing (see above) — may be cut from v0.
- Whether `GitStoreConfig` forwards to a nested `RepoStoreConfig` via
  properties (small diff) or moves fields (clean but churny). Lean: forward.
- `reserved_prefix` default inconsistency (`config.py` default `"root"` vs.
  `GitFxtrNamespace.create` default `"index"`) is pre-existing; out of scope,
  don't fix here.
