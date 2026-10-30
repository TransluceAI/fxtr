# Namespace-backend lease-boilerplate rollback

Follow-up to stage 8b chunk 2. Chunk 2 successfully migrated all external-arena
callers to the new lease-backed design, but overshot on one axis: every
`PersistentPinningBackend` / `IndexablePinningBackend` / `DeepFetchable` method
grew a `lease: Lease[B]` first argument, and every concrete backend grew a
`_with_validated_session_lease` decorator to validate and consume it on each
call. That level of gating is only load-bearing on **arenas**, which *are* the
lifetime-scoped objects. Namespace backends are long-lived session-level
objects; they already know when they're draining (context-manager state) and
can just fail on subsequent calls without needing a caller-supplied lease to
prove the token is still alive.

This plan rolls back the backend-level lease plumbing while keeping every
other piece of chunk 2 (arena leases, brand-parameterized backends, key-lease
plumbing into `lazily_attach_from_arena` / `register_deep_fetch_source`, the
STATIC_LIFETIME / `TransferEntitySource` rewrite, the transfer/hub migration).

## Scope: kept vs. rolled back

### Kept (load-bearing)

- Arena ABCs — `ReadableArena[B]`, `CopyTargetArena[B]`, `ReadWriteArena[B]`,
    `FlatEntitySource[B]` — every method still takes `lease: Lease[B]` first.
    Arenas keep lease gating; that is where lifetimes are enforced.
- `MemoryStore` public lease API, `create_token_from_leases`, the
    `ForeignLeaseData` / foreign-lease plumbing, `LifetimeConverter` protocol,
    `default_lifetime_converter`.
- `FxtrScope._main_lease` / `_main_arena`, `lazily_attach_from_arena(lease,
    node_id, source, key_lease, converter)`, `register_deep_fetch_source(source,
    key_lease, converter)`. Everything about plumbing a *source*-arena lease
    through into the memory store stays.
- `PersistentPinningBackend[B]` / `IndexablePinningBackend[B]` /
    `DeepFetchable[B]` stay **brand-parameterized** and keep their `token`
    property. The backend's own token is still real; what changes is that the
    backend's methods no longer require the caller to present a lease for it.
- `retrieve_pinned` still returns `tuple[FxtrID, ReadableArena[B]]`.
    `list_indexers` / `iter_matches` still return `ReadableArena[B]`. Callers
    obtain the `Lease[B]` they need for those returned arenas via
    `backend.get_lease()` (concrete-class helper; see below).
- Cross-arena methods keep their source-side lease argument:
    `pin_entity_from_arena(key, entity_id, source, src_lease, *, overwrite)`,
    `register_indexer(indexer_id, source, src_lease, key_prefix)`, and
    `IndexComputer.compute_indexer_entries` / `_ensure_indexers_loaded` keep
    `(entity_arena, entity_lease)` and `(indexer_arena, indexer_lease)` pairs.
- Session ULL pattern — `SQLiteFxtrSession` owns the ULL, exposes
    `token` / `grant` / `root_lease`. Every backend / arena produced from the
    session brands on this token.
- `STATIC_LIFETIME`, the two-set `TransferEntitySource`, the widening of
    `pin_entity_from_arena` to accept `FlatEntitySource`, and the entire
    transfer/hub migration from `transfer_and_hub_fix_plan.md` — keep.
- `HttpReadableArena._with_validated_arena_lease` — stays. That decorator is
    on an **arena**, not a backend.

### Rolled back (overzealous boilerplate)

1. **ABC signatures lose the backend-lease arg.**
    - `PersistentPinningBackend[B]`:
      - `pin_entity_from_arena(key, entity_id, source, src_lease, *, overwrite)`
      - `unpin_entity(key)`
      - `retrieve_pinned(key) -> (FxtrID, ReadableArena[B])`
      - `list_pins(prefix) -> dict[str, FxtrID]`
    - `IndexablePinningBackend[B]`:
      - `register_indexer(indexer_id, source, src_lease, key_prefix)`
      - `unregister_indexer(indexer_id, key_prefix)`
      - `list_indexers() -> (list[...], ReadableArena[B])`
      - `iter_matches(first, *rest, key_prefix, where)`
    - `DeepFetchable[B]`:
      - `try_lookup(node_id) -> ReadableArena[B] | None`
    - `SQLiteNamespaceBackend.run_direct_sql(query, views)` — concrete-only
        method but follows the same rule.
2. **Delete `_with_validated_session_lease` entirely.** Remove the helper and
    every decoration site in `sqlite_namespace.py` and `git_namespace.py`.
    Methods no longer validate a caller lease; they instead fail through
    their existing `_closed` / session-active checks.
3. **Remove `validate_lease_grant` + `lease.take()` prologues** from
    `WorkspacePinningBackend` (4 sites) and `HttpDeepFetchable.try_lookup`.
4. **Caller sites stop threading the backend lease.** Every call into a
    backend method drops the first `lease.clone()` argument. `FxtrNamespace`'s
    abstract wrappers (`pin`, `unpin`, `_retrieve_impl`, `list_pins`,
    `register_indexer`, `unregister_indexer`, `list_indexers`,
    `iter_matches`, `sql`) stop passing `self._backend_lease()` into the
    backend. (`_backend_lease()` itself stays — see next section.)

### Unchanged despite being adjacent

- `FxtrNamespace._backend_lease()` — kept. It still mints key-leases passed
    into `lazily_attach_from_arena` (for the source arena the backend returned)
    and `register_deep_fetch_source` (at namespace-entry time). Keeping this
    one helper is what makes the key-lease plumbing consistent across
    concrete namespaces.
- `backend.get_lease()` on concrete `SQLiteNamespaceBackend` /
    `GitNamespaceBackend` — kept as concrete-class helpers. Not promoted to
    the ABC. `FxtrNamespace` subclasses wire `_backend_lease()` to it (or to
    `self._session.root_lease.clone()`) however they like.

## Subtleties worth flagging up front

These don't fit a mechanical pass — they need per-site judgment.

1. **`MemoryStore.try_lookup` does not fit the narrowed `DeepFetchable[B]`
    ABC.** `MemoryStore` serves multiple tokens simultaneously; its
    `try_lookup(lease, node_id)` uses the lease to *select which caller's
    brand is asking* and returns an arena under that caller's brand. That
    lease argument has real semantic content, unlike on a namespace backend
    where it's redundant. Decision: rename the store method to
    `MemoryStore.try_lookup_with_lease(lease, node_id)` and stop claiming
    `DeepFetchable` conformance (the class never explicitly subclassed
    `DeepFetchable` — `memory_arena.py:275` — so this is a docstring / type
    change, not a code-structure change). Update
    `FxtrScope._deep_fetch_cached` to call `_global_store.try_lookup_with_lease(
    self._main_lease.clone(), node_id)` directly. `DeepFetchable[B]` is now
    specifically the ABC for *external* deep-fetch sources registered via
    `register_deep_fetch_source`.

2. **`_deep_fetch_scan_sources` drops the converter call from the try_lookup
    step only.** Currently the scan loop does
    `y_lease = entry.converter(entry.key_lease.clone(), self._main_lease.clone())`
    purely to pass `y_lease` into `try_lookup`. After rollback, the
    converter invocation at that step is deleted — just call
    `await entry.source.try_lookup(node_id)`. The converter is **not** dead:
    it is still used below (same loop) at the `lazily_attach_from_arena`
    step, which threads `entry.key_lease.clone()` and `entry.converter`
    unchanged. Be careful not to over-delete.

3. **Mixed-role `lease.clone()` inside backend method bodies.** In
    `SQLiteNamespaceBackend.pin_entity_from_arena` and
    `GitNamespaceBackend.pin_entity_from_arena`, the incoming `lease`
    parameter is cloned into multiple downstream calls: some as
    *backend-leases* (to sub-backend calls — these just disappear) and
    others as *source-arena key-leases* (for the cross-namespace pin where
    the first arena is passed as *source* to the next — these must survive,
    re-sourced from `self._session.root_lease.clone()`). Example at
    `git_namespace.py:492-494`:
    ```
    await self._worktree_ns.pin_entity_from_arena(
        lease.clone(), key, entity_id, index_arena, lease.clone(), overwrite=overwrite
    )
    ```
    First `lease.clone()` → deleted (backend's own lease). Second
    `lease.clone()` → becomes `self._session.root_lease.clone()` (key-lease
    for `index_arena` as a source). Audit each such site.

4. **`LeaseGrantMismatchError` tests on backend methods must be deleted,
    not rewritten.** Some tests specifically construct a wrong-brand lease,
    pass it to a backend method, and assert `LeaseGrantMismatchError`. The
    contract those tests cover no longer exists on backends. Grep
    `tests/` for `LeaseGrantMismatchError` and delete the ones targeting
    namespace backends; keep any that target arenas (those still validate).

5. **`_with_validated_session_lease` helper has different fates in the two
    namespace files.** See per-file notes below — do not assume both files
    follow the same rule.

## ABC and concrete-class rewrites

### `fxtr2/data/abstract_arena.py`

Drop `lease: Lease[B]` (and rewrite docstrings) on
`PersistentPinningBackend.{pin_entity_from_arena, unpin_entity,
retrieve_pinned, list_pins}` and `DeepFetchable.try_lookup`. Narrow the
`DeepFetchable[B]` class docstring to say "external deep-fetch sources
registered via `FxtrScope.register_deep_fetch_source`" and explicitly note
that `MemoryStore` is *not* a `DeepFetchable` under this narrower contract
(see subtlety #1). Leave the class-level docstrings mentioning that
callers pass *source*-arena leases on methods like `pin_entity_from_arena`.
Leave `validate_lease_grant` and `LeaseGrantMismatchError` in place — still
used by memory-store and arena code.

### `fxtr2/data/memory_arena.py`

Rename `MemoryStore.try_lookup` → `MemoryStore.try_lookup_with_lease`.
Signature and body are unchanged — the rename is purely to free the
`try_lookup` name for the narrowed ABC shape and to make the lease-taking
variant self-describing. Update the class-level docstring to note that the
store does not satisfy `DeepFetchable[B]`; callers use
`try_lookup_with_lease` directly (it is polymorphic over the caller's
brand per call). No other changes.

### `fxtr2/data/store_indexing/indexing_types.py`

Drop `lease: Lease[B]` from `IndexablePinningBackend.{register_indexer,
unregister_indexer, list_indexers, iter_matches}`. Rewrite docstrings.

### `fxtr2/data/sqlite_store/sqlite_namespace.py`

- **Keep the `_with_validated_session_lease` helper definition.** It is
    still used by `SQLiteNamespaceArena` (the arena subclass at the bottom
    of the file), which stays fully lease-gated. Consider renaming it to
    `_with_validated_arena_lease` to reflect the narrowed usage, but that's
    cosmetic.
- Remove `@_with_validated_session_lease` and the `lease: Lease[B]` parameter
    from every `SQLiteNamespaceBackend` method (but **not** from
    `SQLiteNamespaceArena` methods — those stay decorated).
- Each backend method body: watch for the mixed-role `lease.clone()` pattern
    (subtlety #3). Backend-own uses disappear; source-arena key-lease uses
    become `self._session.root_lease.clone()`. The existing `is_active()` +
    `_check_not_read_only()` guards stay; they are what actually fail when
    the session is draining.
- `token` property on `SQLiteNamespaceBackend` stays; `get_lease()` stays.
- The pin/retrieve/register/etc internals that build temporary arenas and
    attach them still need key-leases — they use `self._session.root_lease.clone()`
    directly, same as today's internal code already does in several places
    (e.g. lines 1113, 1122, 1800).

### `fxtr2/data/git_store/git_namespace.py`

- **Delete the `_with_validated_session_lease` helper entirely.**
    `GitNamespaceBackend` has no arena subclass in this file, so there's no
    remaining caller. Do not preserve it here even though the sqlite file
    keeps a similarly-named helper.
- Remove the decorator and `lease: Lease[Any]` param from every method of
    `GitNamespaceBackend` (`pin_entity_from_arena`, `unpin_entity`,
    `retrieve_pinned`, `list_pins`, `register_indexer`, `unregister_indexer`,
    `list_indexers`, `iter_matches`, `run_direct_sql`, `try_lookup`).
- Internal calls to `self._index_ns.*` and `self._worktree_ns.*` drop their
    first `lease.clone()` argument. The *source*-arena lease clones stay
    but get re-sourced to `self._session.root_lease.clone()` — see
    subtlety #3 for the specific pattern at `git_namespace.py:492-494`.
- `token` property and `get_lease()` stay.

### `fxtr2/experimental/http/workspace_namespace.py`

- Drop `validate_lease_grant(...)` + `with lease.take():` prelude in each of
    `WorkspacePinningBackend.{pin_entity_from_arena, unpin_entity,
    retrieve_pinned, list_pins}`. Remove the `lease` parameter.
- `pin_entity_from_arena` still takes and consumes `src_lease` as today.
- `WorkspacePinningBackend.token` stays; the namespace's
    `_backend_lease()` wiring in `WorkspaceNamespace` stays.

### `fxtr2/experimental/http/client.py`

- `HttpDeepFetchable.try_lookup` drops `lease: Lease[Any]` and the
    `validate_lease_grant` / `lease.take()` prelude. Body keeps the rest.
- `HttpReadableArena` is untouched — it's the arena, stays lease-gated via
    `_with_validated_arena_lease`.

### `fxtr2/data/fxtr_context_api.py`

In every `FxtrNamespace`-abstract method, drop the
`self._backend_lease()` argument that was being passed as the *backend's
own* lease to `self._get_backend().<method>(...)`. Affected methods: `pin`,
`unpin`, `_retrieve_impl`, `list_pins`, `register_indexer`,
`unregister_indexer`, `list_indexers`, `iter_matches`, `sql`. The
`_backend_lease()` call still appears where we're minting a **key-lease for
a source arena**: inside `_retrieve_impl`'s `lazily_attach_from_arena`, and
in `register_indexer`/`list_indexers`/`iter_matches` where we attach the
result arena into `my_scope._main_arena`.

Concrete namespaces (`SQLiteFxtrNamespace`, `GitFxtrNamespace`,
`WorkspaceNamespace`) keep their `_backend_lease()` implementations as-is
(cloning from the session or backend-lifetime ULL).

### `fxtr2/data/entity_ref_scope.py`

- `_deep_fetch_cached`: change
    `self._global_store.try_lookup(self._main_lease.clone(), node_id)` to
    `self._global_store.try_lookup_with_lease(self._main_lease.clone(),
    node_id)` (rename call site to match MemoryStore's new method name —
    subtlety #1). Lease is still threaded; this is a pure rename.
- `_deep_fetch_scan_sources`: delete the
    `y_lease = entry.converter(entry.key_lease.clone(),
    self._main_lease.clone())` line and change
    `await entry.source.try_lookup(y_lease, node_id)` to
    `await entry.source.try_lookup(node_id)`. **Do not touch** the attach
    block below it: the `lazily_attach_from_arena` call still uses
    `entry.key_lease.clone()` and `entry.converter` — the converter is
    only dead at the *try_lookup* step, not at attach (subtlety #2).

### `fxtr2/data/store_indexing/index_computer.py`

No signature changes here. IndexComputer methods already take
`(entity_arena, entity_lease)` and `(indexer_arena, indexer_lease)` pairs
for *source* arenas — those stay. Callers in `SQLiteNamespaceBackend` that
already pass `self._session.root_lease.clone()` as `indexer_lease` continue
to do so.

### `fxtr2/legacy_compat/`

Grep for backend calls; drop backend-lease args at each call site.
`UpgradeCache` still has a token and a `_backend_lease`-style hook for the
arena it exposes, but the arena gets leases (it's an arena) while
UpgradeCache-as-pinning-backend (if any) drops them. The migration there
is mechanical.

## Test-file updates (mechanical)

These call one or more of the ABC-level backend methods and will need their
call sites updated to drop the `lease.clone()` first argument. Not
describing the exact rewrite per file — it's a purely mechanical edit.

- `tests/test_fxtr_context_api.py`
- `tests/test_sqlite_store/test_readonly_mode.py`
- `tests/test_sqlite_store/test_sqlite_store.py`
- `tests/test_entity_ref_scope/test_experimental_deep_fetch.py`
- `tests/test_store_indexing/test_user_facing_api.py`
- `tests/test_store_indexing/test_backend_integration.py`
- `tests/test_experimental/test_http/test_client.py`
- `tests/test_experimental/test_http/test_workspace_namespace.py`
- `tests/test_git_store/_patch_git_ns_tests.py`
- `tests/test_git_store/test_git_namespace.py`
- `tests/test_git_store/test_git_namespace_indexing.py`
- `tests/test_git_store/test_git_namespace_staleness.py`
- `tests/test_git_store/test_sync.py`
- `tests/test_git_store/test_index_sync.py`
- `tests/test_git_store/test_cli.py`
- Any `tests/test_memory_arena.py` sites that assembled mock backends with
    lease arguments (should be small).
- `fxtr2/data/git_store/sync.py`, `git_store/index_sync.py`,
    `fxtr2/cli/git_store_cli.py` — non-test, internal call sites that
    mirror the namespace API; update the same way.
- Examples: `examples/simple_store_and_load.py`, `examples/store_indexing/*`,
    `examples/session_manager_porting/v2_version/run_demo.py`,
    `examples/simple_copy_server/copy_server.py`,
    `examples/server_demo/*`, `examples/benchmarks/store_benchmarks/cases.py`,
    scratch notebooks. Mechanical.

**Before the mechanical test pass**, grep `tests/` for
`LeaseGrantMismatchError` and audit each hit. Tests that pass a wrong-brand
lease to a backend method (`pin_entity_from_arena`, `retrieve_pinned`,
`try_lookup`, `register_indexer`, etc.) and assert the error must be
**deleted** — that contract no longer exists on backends. Tests that target
*arena* methods (e.g. `SQLiteNamespaceArena.load`, `HttpReadableArena.load`,
`MemoryArena.load`) stay unchanged. This is the one test-pass item that is
not safely mechanical.

## Implementation order

Suggested ordering (each step leaves the tree broken until the next):

- [x] **Step 1 — ABC signatures + MemoryStore rename.** Updated
    `abstract_arena.py` (`PersistentPinningBackend` / `DeepFetchable` lose
    the lease-first arg; `DeepFetchable` docstring narrowed to "external
    deep-fetch sources"), `store_indexing/indexing_types.py`
    (`IndexablePinningBackend` methods + `run_direct_sql`), and renamed
    `MemoryStore.try_lookup` → `try_lookup_with_lease` in `memory_arena.py`
    with a docstring note that the store is not `DeepFetchable`.
- [x] **Step 2 — `SQLiteNamespaceBackend`.** Lease args + decorators
    removed from backend methods. Helper renamed
    `_with_validated_session_lease` → `_with_validated_arena_lease` (scoped
    to `SQLiteNamespaceArena` only). Internal key-leases sourced via
    `self._session.root_lease.clone()`.
- [x] **Step 3 — `GitNamespaceBackend`.** Decorator + helper fully deleted.
    Internal sub-namespace calls dropped the backend-lease arg; source-arena
    key-leases re-sourced from `self._session.root_lease.clone()` at the
    one call site that needed it.
- [x] **Step 4 — `WorkspacePinningBackend` + `HttpDeepFetchable`.**
    Validate+take preludes and lease params removed; `validate_lease_grant`
    import dropped from `workspace_namespace.py`. `HttpReadableArena`
    untouched (still `_with_validated_arena_lease`-decorated).
- [x] **Step 5 — `fxtr_context_api.py`.** `FxtrNamespace` wrapper methods
    stopped passing `self._backend_lease()` as the backend-lease arg
    to `pin_entity_from_arena` / `unpin_entity` / `retrieve_pinned` /
    `list_pins` / `register_indexer` / `unregister_indexer` /
    `list_indexers` / `iter_matches` / `run_direct_sql`.
    `_backend_lease()` survives and is still used for source-arena
    key-leases (into `lazily_attach_from_arena`).
- [x] **Step 6 — `entity_ref_scope.py`.** `_deep_fetch_cached` now calls
    `try_lookup_with_lease(self._main_lease.clone(), node_id)`.
    `_deep_fetch_scan_sources` deleted the `y_lease = converter(...)` line
    and now calls `entry.source.try_lookup(node_id)`; the subsequent
    `lazily_attach_from_arena` still threads `entry.key_lease.clone()` and
    `entry.converter` unchanged.
- [x] **Step 7 — `legacy_compat/`, `git_store/sync.py`,
    `git_store/index_sync.py`, `cli/git_store_cli.py`.** All call sites
    either use `FxtrNamespace`-level APIs (no lease to begin with) or are
    backend-level calls with only the source-arena `src_lease` preserved.
    No surviving backend-lease args.
- [x] **Step 8 — Tests.** First: grep `tests/` for
    `LeaseGrantMismatchError`, delete tests whose wrong-brand lease
    targets a backend method (see the test-file section). Then mechanical
    pass over the file list above. Run `uv run pytest -q`.
    *(Grep: no `LeaseGrantMismatchError` in `v2/tests/`.)*
- [x] **Step 9 — `projects/behaviors_hub_*`.** `entity_service.py` and
    `store_sync.py` had their `self.get_lease()` / session-lease
    first-args removed.
- [x] **Step 10 — Examples + scratch notebooks.** No changes needed: every
    call site in `examples/` and `scratch_notebooks/` uses the
    `FxtrNamespace` wrapper API (e.g. `ns.list_pins()`, `ns.register_indexer(...)`),
    which did not take a backend-lease even before the rollback. Verified
    via grep for the affected method names.
- [x] **Step 11 — Update CODE_OVERVIEW.md and LOCAL_CODE_OVERVIEW.md files.**
    `CODE_OVERVIEW.md` (abstract_arena section, memory-store try_lookup
    bullet, deep-fetch-sources paragraph), `sqlite_store/`, `git_store/`,
    and `experimental/` LOCAL overviews updated. `store_indexing/`,
    `remote/`, `leases/`, `legacy_compat/` checked and needed no changes.
    `overview.md` now has the rollback pointer at the top.
- [x] **Step 12 — Full test run.** `uv run pytest -q` → 1733 passed in 23s.

## Cross-reference back to `overview.md`

Done — a blockquote was added directly under the `# Stage 8b` header in
`overview.md` pointing at this rollback doc and noting that the chunk-2
checkmarks are retained as a historical record.

## Remaining documentation work (step 11)

The following doc files still describe the pre-rollback contract and need
updating. Each entry lists the *specific* stale lines to catch.

### `CODE_OVERVIEW.md`

- Line ~344: `PersistentPinningBackend[B]`: `retrieve_pinned(lease, key)` →
    `retrieve_pinned(key)`.
- Line ~345: `DeepFetchable[B]`: `try_lookup(lease, node_id)` →
    `try_lookup(node_id)`. Add note that `MemoryStore` is *not* a
    `DeepFetchable` — it exposes the polymorphic `try_lookup_with_lease`
    instead.
- Line ~385 (deep-fetch sources paragraph): "calls
    `converter(key.clone(), self._main_lease.clone())` to produce a fresh
    `Lease[Y]` for each `source.try_lookup(y_lease, node_id)`" is stale.
    After rollback, `try_lookup(node_id)` takes no lease; the converter is
    used only at the `lazily_attach_from_arena` step on a hit.
- Line ~486 (MemoryStore bullet): `try_lookup(lease, node_id)` → rename to
    `try_lookup_with_lease(lease, node_id)`, and drop the "matches
    `DeepFetchable[B]`" wording.
- Add a brief one-sentence callout under the arenas/backends section
    restating that arenas are the lease-gated tier and namespace backends
    manage their own drain.

### `fxtr2/data/sqlite_store/LOCAL_CODE_OVERVIEW.md`

- Line ~234: drop "lease-first APIs; session grant validated via
    `_with_validated_session_lease`" — the helper was renamed to
    `_with_validated_arena_lease` and applies only to
    `SQLiteNamespaceArena`, not to `SQLiteNamespaceBackend`.
- Line ~237: `try_lookup(lease, node_id)` → `try_lookup(node_id)`.
- Anywhere pin/retrieve/list-pins are described as lease-first, rewrite
    to "no backend lease; arena-level methods on the returned arena still
    take a `Lease[B]`".

### `fxtr2/data/git_store/LOCAL_CODE_OVERVIEW.md`

- Line ~21: drop "lease-first pin/retrieve/indexing APIs; session `grant`
    validation via `_with_validated_session_lease`" — the helper was fully
    deleted from this file.
- Pseudo-code block around lines 572-596 (method signatures): drop the
    `lease: Lease[Any]` parameter from `pin_entity_from_arena`,
    `unpin_entity`, `retrieve_pinned`, `list_pins`, `try_lookup`.
    (Example callers around lines 634-640 are already correct — they show
    the `FxtrNamespace`-level API.)

### `fxtr2/experimental/LOCAL_CODE_OVERVIEW.md`

- Line ~39: drop "calls `converter(...)` + `source.try_lookup(y_lease, ...)`"
    pattern; the scan now calls `source.try_lookup(node_id)` and the
    converter runs only at the attach step.
- Line ~64: `try_lookup(lease, node_id)` → `try_lookup(node_id)` for
    `HttpDeepFetchable`.
- Line ~72: `WorkspacePinningBackend` bullet — drop the
    "`validate_lease_grant(self._lifetime.root_lease.grant, lease)` on
    entry" sentence; the backend no longer validates a caller lease.
- `HttpFxtrReadNamespace` / `WorkspaceNamespace` bullets: still correct
    — they `register_deep_fetch_source(..., root_lease.clone())` and
    `_backend_lease()` is still used for source-arena key-leases.

### `fxtr2/data/store_indexing/LOCAL_CODE_OVERVIEW.md`

IndexComputer's internal API is unchanged (still takes
`entity_arena/entity_lease` and `indexer_arena/indexer_lease` pairs as
source-arena leases). Verify that nothing else in the file mentions
"lease-first backend methods" or `_with_validated_session_lease`.

### `fxtr2/data/remote/LOCAL_CODE_OVERVIEW.md`

Only touches `TransferEntitySource`, which is a `FlatEntitySource[StaticBrand]`
(arena tier) — lease args on its methods stay. Should already be correct;
spot-check.

### `fxtr2/leases/LOCAL_CODE_OVERVIEW.md`

Nothing to change — leases themselves are unaffected. Spot-check.

### `plans/phase3/stage8b_leased_memory_store/overview.md`

Add the rollback pointer near the top (see "Cross-reference back to
`overview.md`" above). Do not edit the chunk-2 checkmarks.

## After docs: full test run (step 12)

`uv run pytest -q` from `v2/`. Expected baseline ≈ 1613 passing (the
number from the end of chunk 2). Investigate any regression.
