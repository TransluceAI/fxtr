# Stage 7: Memory Store Refactor — Lifetime Tokens and Global Store

## Overview

This refactor replaces the current per-scope `MemoryStore` architecture with a single global `MemoryStore` backed by a **lifetime token** system for managing entity lifetimes. The goals are:

1. **Eliminate redundant copying** when moving data between scopes (e.g. child → parent). Currently this requires async loads and full data copying because each scope has its own store. With a shared store, many of these become cheap metadata operations.
2. **Enable future offloading** of in-memory data back to external stores.
3. **Reduce per-scope overhead** from allocating a separate store for every scope.
4. **Lay groundwork** for `track_from_scope` (sync data transfer) and weak scopes.

The refactor is split into three phases:
- **Phase 1**: Global store + lifetime tokens (preserves scope semantics; minimal test breakage)
- **Phase 2**: `track_from_scope` (new sync operation for transferring data between scopes)
- **Phase 3**: Token sharing optimization + weak scopes

---

## Key Design Decisions

These were established during the design discussion and should be treated as constraints.

### Single global MemoryStore
- Replace `GLOBAL_BACKING_STORE` contextvar with `GLOBAL_MEMORY_STORE` contextvar holding the entire `MemoryStore`.
- Inline bytes storage into the store (drop `InMemoryBytesBackingStore` and `LMDBBytesBackingStore`; LMDB is never used in practice and is simple to re-add later if needed).
- All scopes share the same store. Scopes no longer create or own a `MemoryStore`.

### Lifetime tokens
- A **lifetime token** represents the lifetime of an external resource or scope.
- Tokens form a **DAG** (multiple parents allowed), mirroring how scopes can have multiple parents.
- **Parent tokens always outlive child tokens**, enforced at runtime by the scope keep-alive mechanism. The store verifies this: invalidating a token while any child token is still active is an internal error.
- Each scope creates exactly one token (phase 1). Arenas are associated with a token; multiple arenas can share a token (e.g. forked arenas for SQLite pinning).

### Unified node representation
- Replace `StoredNode` / `DerivedNode` / `InheritedNode` with a single `MemoryStoreNode` type.
- Each node tracks: optional in-memory data (`NodeData`), a map from token → inheritance arena, and an **antichain** of lifetime tokens representing direct claims.

### Antichain-based lifetime tracking
- A token has a **claim** on a node if:
  - **Case 1 (inheritance):** A parent token has a claim.
  - **Case 2 (external resource):** The token has an inheritance arena registered for this node.
  - **Case 3 (in-memory):** The node has in-memory data AND all of the node's deps are claimed by this token.
- The **antichain** stores the set of tokens with direct claims (case 2 or 3). Case 1 is implicit from the token hierarchy.
- Each token maintains a `set[FxtrID]` of nodes where it appears in the antichain, for efficient cleanup.

### Store operation: initial antichain = {T}
- When storing a new node in an arena with token T, the initial antichain entry is just `{T}`.
- Token management is handled entirely by the store, not by arenas. `bare_store` accepts a token parameter and handles antichain assignment internally. Arenas pass their token through to the store.
- We do NOT eagerly propagate claims to ancestor tokens. This means:
  - Data stored in a child scope is freed when the child scope closes (matching current behavior).
  - To keep data alive at a parent level, explicitly copy/track it up.
  - The total work for upward propagation is the same, just deferred.
- When storing into a node that already exists (dedup), add T to the existing antichain (with normal antichain simplification: remove dominated elements).

### Inheritance arena invariant
- We maintain the invariant that a node only holds inheritance arena entries for tokens that are in its antichain. If adding a new token to the antichain causes an existing token to be dominated and removed, its inheritance arena entry should also be released (abandoned and removed from the map). This keeps the inheritance arena map compact and avoids holding onto external resource claims that are no longer load-bearing.

### GC model
- When a token T is invalidated: remove T from all antichains. Nodes with empty antichains are freed.
- **No cascading needed.** The child-before-parent invalidation order guarantees that if node A (with token T₂) depends on node B (with token T₁, where T₁ is ancestor of T₂), then T₁ cannot be invalidated while T₂ is still active. So B cannot be freed while A still needs it.
- **No `required_by` needed** for node-to-node deps. The antichain + token ordering invariant is sufficient.
- Arena livesets are still needed for access control (is_tracked), but not for GC. A node's antichain becoming empty guarantees it's not in any active arena's liveset (by the same ordering argument).
- Nodes that are abandoned from an arena but still claimed by the arena's token will linger until the token is invalidated. This is acceptable; we can optimize later if needed.
- `wrapped_by` is purely for backrefs (`ObservableStore`), not for keeping nodes alive. A node is never kept alive solely by `wrapped_by`; it must have a token in its antichain. When GC'ing a node, we clean up `wrapped_by` on its unwrapped node, but this never triggers further GC (the unwrapped node's own antichain tokens are guaranteed to still be active, since they are ancestors of the freed node's tokens).

### Scopes maintain their own parent tracking
- Scopes continue to track `_parents` and `_all_ancestor_scope_ids` for inclusion checks, independent of the token hierarchy.
- This is necessary because in phase 3, multiple scopes may share the same token but have different nesting relationships.
- Tokens may separately maintain `_all_ancestor_ids` for efficient is-ancestor checks.

### Per-scope caches remain
- The `_ref_loaded_value_cache` stays per-scope. The global store does not need to know about caching.

---

## Data Structures

### TokenKey

```python
type TokenKey = int
```

A type alias for `int`, used as the unique identifier for a `LifetimeToken`. Makes token IDs self-documenting in type annotations (e.g. `dict[TokenKey, ReadableArena]` instead of `dict[int, ReadableArena]`).

### LifetimeToken

```python
@dataclasses.dataclass
class LifetimeToken:
    """Represents the lifetime of an external resource or scope."""
    key: TokenKey                              # unique ID within the store
    parents: tuple[LifetimeToken, ...]         # direct parents in the DAG
    all_ancestor_keys: frozenset[TokenKey]     # precomputed: keys of all ancestor tokens
    nodes_in_antichain: set[FxtrID]            # reverse index: nodes claiming this token
    active: bool = True
    children: set[LifetimeToken]               # direct children (for invalidation check)
```

`all_ancestor_keys` is computed at creation time as the union of each parent's `all_ancestor_keys` plus the parent keys themselves. This enables O(1) is-ancestor checks: `ancestor.key in self.all_ancestor_keys`.

### NodeData

```python
@dataclasses.dataclass
class NodeData:
    """In-memory loaded data for a node (replaces StoredNode + DerivedNode)."""
    entity_bytes: bytes | None            # CBOR bytes (d/n nodes), None for derived
    requires: frozenset[FxtrID]           # direct dependency IDs
    strong_linked_refs: frozenset[FxtrID] # strong links (d/n nodes only)
    unwraps_to: FxtrID | None
    ctx_node: FxtrID | None               # c/p nodes
    backstep_node: FxtrID | None          # p nodes
```

Whether a node is "stored" vs "derived" is determined by whether `entity_bytes` is `None`. The `d`/`n` vs `c`/`p`/`u` distinction comes from the node ID's tag.

### MemoryStoreNode

```python
@dataclasses.dataclass
class MemoryStoreNode:
    """A node in the global memory store (replaces StoredNode/DerivedNode/InheritedNode)."""
    my_id: FxtrID
    data: NodeData | None                              # None if only known via inheritance
    unwraps_to: FxtrID | None
    inheritance_arenas: dict[LifetimeToken, ReadableArena]  # case 2 claims (token → arena)
    owned_tokens: set[LifetimeToken]                        # case 3 claims
    wrapped_by: set[FxtrID]                                 # for backrefs (ObservableStore)

    @property
    def antichain(self) -> set[LifetimeToken]:
        """Full antichain: union of case 2 (inheritance) and case 3 (owned) tokens."""
        return self.owned_tokens | set(self.inheritance_arenas.keys())
```

The **antichain** is a computed property — the union of `owned_tokens` and `inheritance_arenas` keys. There is no stored `antichain` field.

- **`owned_tokens`** (case 3): The token's scope has fully resolved this node — data is in memory and all deps are also claimed by this token. `_ensure_own` checks this set (not data presence) to decide whether work is needed.
- **`inheritance_arenas`** (case 2): External resource backing. The token has an arena that can provide this node's data. Re-keyed from `TokenKey` to `LifetimeToken` for direct access.

`_ensure_own` checks `owned_tokens` to decide whether work is needed. This correctly handles the case where data exists from a different token but deps haven't been claimed by the requesting token — the old `InheritedNode` equivalent is "not in `owned_tokens` for this token."

---

## Phase 1: Global Store + Lifetime Tokens

This phase moves everything to the global store and token system without changing scope semantics. Existing tests should continue to pass, with possible exceptions for tests that directly inspect `MemoryStore` or `MemoryArena` internals.

### Step 1: LifetimeToken implementation

Add a `LifetimeToken` class to `memory_arena.py` (or a new `lifetime.py` alongside it) with:

- `create_token(parents: Sequence[LifetimeToken]) -> LifetimeToken` — validates all parents are active, computes `all_ancestor_ids`, registers with parents' `children` sets.
- `invalidate_token(token)` — verifies no active children exist (raise if violated), walks `nodes_in_antichain` to remove the token from each node's antichain, frees nodes with empty antichains, marks token inactive.
- `is_ancestor(possible_ancestor, token) -> bool` — `possible_ancestor.token_id in token.all_ancestor_ids`.
- `is_claimed_by(node, token) -> bool` — true if token is in `node.antichain` OR any ancestor of token is in `node.antichain`.

### Step 2: Unified MemoryStoreNode + NodeData

Replace `StoredNode`, `DerivedNode`, `InheritedNode` with `MemoryStoreNode` and `NodeData`. This is a mechanical replacement inside `MemoryStore`:

- Where code checks `isinstance(node, InheritedNode)` → check `not _has_case3_access(node, token)` (the node may or may not have data from another token; what matters is whether *this* token has fully resolved ownership).
- Where code checks `isinstance(node, StoredNode)` → check `node.data is not None and node.data.entity_bytes is not None`
- Where code checks `isinstance(node, DerivedNode)` → check `node.data is not None and node.data.entity_bytes is None`
- `_already_has_data_for(node_id)` → `node.data is not None`
- `_save_or_upgrade_node` becomes: populate `node.data` on an existing `MemoryStoreNode` (or create a new one)

Remove `required_by` from the node type entirely. GC is token-based, not reference-count-based.

### Step 3: Refactor MemoryStore to global singleton

Key changes:

- **GLOBAL_MEMORY_STORE contextvar** replaces `GLOBAL_BACKING_STORE`. Holds a `MemoryStore` instance.
- **MemoryStore** becomes the single source of truth for all in-memory entity data. It owns:
  - `_entity_states: dict[FxtrID, MemoryStoreNode]`
  - `_token_counter: int` (for unique token IDs)
  - `_arena_counter: int`
  - `_arenas: dict[ArenaKey, MemoryArenaState]`
  - `_bytes_data: dict[bytes, tuple[bytes, int]]` — raw bytes + refcount (inline)
  - Token management methods: `create_token`, `invalidate_token`
- **Remove** `InMemoryBytesBackingStore` and `LMDBBytesBackingStore`. Bytes are stored inline in `_bytes_data`. LMDB is not used in practice and is straightforward to re-add later.
- **`bare_store(entity, token)`**: accepts a token parameter and handles all antichain management internally. When creating a new `MemoryStoreNode`, sets `antichain = {token}`. When storing into an existing node (dedup), adds `token` to the existing antichain (with domination cleanup: remove dominated tokens, and release their inheritance arena entries per the inheritance arena invariant). Also stores `wrapped_by` links on unwrapped nodes. All token management lives in the store — arenas just pass their token through.
- **`bare_lazily_attach_from_arena`**: instead of creating an `InheritedNode`, creates a `MemoryStoreNode` with `data=None` and adds the inheritance arena to `inheritance_arenas` keyed by the provided token. Also tracks `unwraps_to` (by querying the source arena's `unwrap_provenance`).
- **`_adopt_inheritance_arena_claim`** → simplified. If node already exists with data and the new token is dominated by an existing antichain entry, abandon the claim on the source arena (not needed). If the new token extends the antichain, add the inheritance arena entry keyed by the new token, and clean up any dominated tokens' inheritance arena entries. If node doesn't exist, create a new `MemoryStoreNode` with the inheritance arena entry and add the token to the antichain.
- **`bare_load(node_id, token)`**: takes a token parameter (the requesting arena's token) so it can select the right inheritance arena. If `data is not None`, return from memory. If `data is None`, pick an inheritance arena from `inheritance_arenas` whose token is an ancestor of (or equal to) the requesting token, load from it, populate `data`, then propagate the arena's token claim to children (by adding inheritance arena entries for child nodes loaded from the external source).
- **GC**: remove `_dead_pre_cleanup`, `_fresh_nodes`, `_collect_one_garbage`. GC is handled entirely by `invalidate_token`, which walks `nodes_in_antichain` and frees nodes with empty antichains. Freeing a node: free bytes data (if stored), abandon from all inheritance arenas, remove from `_entity_states`, remove from `wrapped_by` of unwrapped node.
- **`close()`**: validates no active tokens, frees all remaining state.

### Step 4: Refactor MemoryArena for token association

- `MemoryArena` gains a `token: LifetimeToken` field (set at creation time).
- `new_arena(token: LifetimeToken) -> MemoryArena` replaces `new_empty_arena()`.
- **`store(entity)`**: calls `bare_store(entity, self.token)` (which handles antichain internally), then `track_in_arena`.
- **`load(node_id)`**: validate that the node is claimed by this arena's token (via `is_claimed_by`). Load data (from memory or inheritance arena). Track children in arena (as now). When loading from an inheritance arena: propagate that arena's token claim to newly-discovered children.
- **`unsafe_lazily_attach_from_arena(entity_id, source)`**: calls `bare_lazily_attach_from_arena` with this arena's token, then `track_in_arena`. (The token is used to key the inheritance arena entry.)
- **`copy_from_arena(entity_id, source)`**: Drop the old same-store fast path entirely. Use the same cross-store logic (`_build_tmp_arena_copying_from_external_arena` pattern) for all copies. In a single global store, this logic naturally becomes fast for the common case: the DAG walk visits each node and checks if it's already claimed by the target token; if so, it stops. If the data is already in-memory with a valid lifetime, no loading is needed. The unified path is simpler and avoids maintaining two code paths. The store handles antichain extension internally as nodes are discovered.
- **`abandon(node_id)`**: remove from liveset (as now). No antichain changes; node stays claimed by the token until the token is invalidated.
- **`close()`**: remove all liveset entries. Arena is gone, but its token may still be active (other arenas or the scope might still use it).
- **`copy_to_new_arena(node_ids)`**: creates a new arena with the **same token** as this arena, and tracks the given nodes. (This is used for forking arenas, e.g. for SQLite pinning or RPC.)

### Step 5: Refactor FxtrScope

Key changes:

- **`__init__`**: no longer creates a `MemoryStore`. Instead:
  - Gets the global store from `GLOBAL_MEMORY_STORE.get()`.
  - Creates a `LifetimeToken` whose parents are the tokens of this scope's parent scopes.
  - Creates a main arena associated with this token.
  - Tracks the context marker in the arena.
- **`_store`**: renamed internal reference. The scope no longer owns `self._store`; it references the global store.
- **`__asynccontextmanager__` (exit)**: instead of closing the store, invalidates the scope's token (which GC's all nodes only claimed by this scope). Closes the main arena. If the token was detached (via `_detach_token`), skip token invalidation — the detaching code takes responsibility.
- **`lift`**: walks parent scopes (as now) to find the node. Since all arenas share the same global store, lift simplifies to: find the node in a parent scope's arena, then `self._main_arena._track_from_store(node_id)`. No inheritance arena or data copying needed. `track_in_arena` always validates the claim (checking `is_claimed_by` for the arena's token) — this is an important invariant check, not just a debug assertion. If the claim check fails, that's an internal error.
- **`copy_from_scope`**: calls `self._main_arena.copy_from_arena(node_id, source._main_arena)`, which does the DAG walk and claim extension described in step 4.
- **`store`**: delegates to entity._fxtr_store_impl as now. The scope's arena is active, so stores go through the arena's `store()` method, which handles antichain assignment.
- **`store_cbor`**: lifts all child IDs (as now), then stores through the arena. The arena assigns the scope's token to the new node's antichain.
- **`abandon`**: removes from arena liveset (as now). No antichain changes.
- **`_detach_token`** (replaces `_detach_store`): Detaches the scope's lifetime token so that it is NOT invalidated when the scope exits. The caller takes ownership and is responsible for invalidating the token when done. This directly mirrors the old `_detach_store` semantics — the detacher was responsible for closing the store, now they're responsible for invalidating the token. Lifetime ordering is maintained because the detached token's parents (held by the RPC context scope) will remain alive as long as the RPC context is open.
- **`is_tracked`**: checks the main arena's liveset (as now).
- **`included_in_scope`**: checks `_all_ancestor_scope_ids` (as now). Token hierarchy is consistent but separate.

### Step 6: Update external resource integration

**SQLiteFxtrNamespace / GitFxtrNamespace**: These create a FxtrScope, which now creates a token in the global store. Entities from SQLite/Git get lazily attached via `unsafe_lazily_attach_from_arena` with the namespace scope's token. When the namespace closes, the scope's token is invalidated, freeing all entities only claimed by that token. No changes needed to the namespace API surface.

**RPC (`fxtr_rpc_local.py`)**: Replace `_detach_store()` usage with `_detach_token()`. When a call returns an arena cap, detach the call scope's token so it isn't invalidated on scope exit. The RPC context takes ownership: when the arena cap is released, it invalidates the detached token. The `_detached_stores` set becomes a `_detached_tokens` set on the RPC context, and the cleanup callback calls `invalidate_token` instead of `store.close(close_arenas=True)`.

**HTTP (`experimental/http/client.py`)**: Similar to namespaces — entities are attached with the scope's token. No significant changes.

### Step 7: Update tests

- **`test_memory_arena.py`** (1239 lines): Significant updates needed. Tests directly construct `MemoryStore` instances and inspect `StoredNode`/`DerivedNode`/`InheritedNode`. These need to be rewritten against the new API. The test patterns (store, load, GC, inheritance) remain valid but the assertions change.
- **`test_entity_ref_scope/test_scope_lifecycle.py`** (720 lines): Should mostly pass as-is, since the scope API is unchanged. May need minor updates if tests inspect internals like `_store`.
- **`test_entity_ref_scope/test_store_load.py`** (359 lines): Should mostly pass.
- **`test_entity_ref_scope/test_caching.py`** (409 lines): Should pass (caching is unchanged).
- **`test_fxtr_context_api.py`** (791 lines): Should pass (namespace API unchanged).
- **Other test files**: Should generally pass since they test higher-level APIs.

### Step 8: Update CODE_OVERVIEW.md

Reflect the new architecture: global store, lifetime tokens, unified node type, simplified GC.

---

## Phase 2: `track_from_scope`

### Motivation

`track_from_scope` is a sync alternative to `copy_from_scope` for transferring data between scopes. It walks the entity DAG and extends lifetime claims without loading data from external resources. This is useful for the common pattern of building a value in a sub-scope and transferring it back to a parent scope when you know everything is in-memory.

### Design

**`FxtrScope.track_from_scope(ref, source_scope)`** (sync):
- Walks the DAG of nodes rooted at `ref.entity_id` in the global store.
- For each node encountered:
  - If already claimed by `self`'s token → stop (we're done for this subtree).
  - If the node has in-memory `data`: recursively track all deps. If all deps succeed, add `self`'s token to this node's antichain (case 3 claim). Track the node in `self`'s main arena.
  - If the node does NOT have in-memory data and is NOT claimed by `self`'s token → **error**. This means we've hit a reference to an external resource that can't be resolved synchronously.
- On error: the operation must leave the store in a consistent state. Since we only expand antichains (which preserves all invariants), partial progress is safe — some nodes may have gained claims that they didn't have before, but this is harmless (they'll live slightly longer, and the claims are valid).
- Cache entries from the source scope are copied to this scope (same as `copy_from_scope`).

**Implementation location**: method on `MemoryStore` (or `MemoryArena`), exposed via `FxtrScope`.

### Steps

- [x] 2.1: Implement `track_from_memory_arena` DAG walk on MemoryStore (store-level) + MemoryArena (validates same store, dispatches)
- [x] 2.2: Expose as `FxtrScope.track_from_scope(ref_or_id, source_scope)` with cache copying (lightweight wrapper around MemoryArena.track_from_memory_arena)
- [x] 2.3: Tests for track_from_scope: basic cases, deps, survives source close, cache copying, error on external resource, error on different store, error on untracked source, frozen scope check
- [x] 2.4: Update CODE_OVERVIEW.md

---

## Phase 3: Token Sharing + Weak Scopes

### Token Sharing

**Motivation**: Not every scope needs its own token. If a scope's parents all share the same token, and the scope isn't associated with a new external resource, it can reuse the parent's token. This reduces the number of tokens and simplifies cleanup.

**Rules for sharing**:
- A scope can share its parent's token if **all** parent scopes have the same token, AND the scope was not created by an internal helper that declares it needs a new token (e.g. namespace scopes, RPC scopes).
- The scope still creates its own arena (associated with the shared token) for tracking its own liveset.
- On scope exit: close the arena, but do NOT invalidate the token (it's owned by the parent scope).

**Implementation**:
- Add a `_needs_own_token: bool` flag (or similar) to FxtrScope's init, defaulting to False.
- Namespace scopes, RPC scopes, and isolated scopes set `_needs_own_token = True`.
- In `__init__`, check if all parents have the same token and `_needs_own_token` is False. If so, reuse the parent's token. Otherwise, create a new token.
- Track whether this scope "owns" its token (created it) vs "borrows" it. Only the owner invalidates on exit.

### Weak Scopes

**Motivation**: A weak scope doesn't hold a keep-alive on its parent, but instead registers to be closed when its parent closes. This is useful for long-lived scopes that shouldn't prevent their parent from shutting down but should automatically clean up when the parent does.

**Rules**:
- A weak scope has exactly one parent scope and MUST share its parent's token (no new token).
- It does NOT hold a keep-alive on its parent. Instead, the parent maintains a list of weak children and closes them when it exits.
- Proper (non-weak) child scopes CAN inherit from a weak scope. They keep the weak scope alive by registering a keep-alive on the weak scope's non-weak parent (following the chain up until reaching a non-weak scope). This ensures the non-weak parent doesn't close while grandchildren are active.
- If the non-weak parent closes (which forces the weak scope to close), and the weak scope has proper child scopes keeping it alive, those children prevent the parent from closing (same as current keep-alive semantics applied to the non-weak grandparent).

**Implementation**:
- Add `weak: bool` parameter to FxtrScope.__init__. Weak scopes must not be isolated and must have exactly one parent.
- Weak scopes share their parent's token and do not register a keep-alive on the parent.
- Instead, the parent registers the weak scope in a `_weak_children` list.
- On parent exit: close all weak children before proceeding with normal shutdown.
- When a non-weak child scope enters and has a weak scope as parent: follow the weak-parent chain to find the first non-weak ancestor and register keep-alive there.

### Steps

- [x] 3.1: Implement token sharing logic in FxtrScope.__init__ (`_share_token` parameter, default is own token)
- [x] 3.2: Tests for token sharing (verify shared vs own token, entity survival, error on different parents, error on no parents, isolated always owns)
- [x] 3.3: Implement weak scopes (`new_weak_child_scope()` on parent, immediate startup, parent closes weak children on exit, non-weak grandchildren keep-alive nearest non-weak ancestor)
- [x] 3.4: Tests for weak scopes (create, store/load, closed on parent exit, doesn't prevent parent exit, cannot be entered, nested weak scopes, grandchild keeps grandparent alive, scope inclusion)
- [x] 3.5: Update CODE_OVERVIEW.md

---

## Phase 1 Implementation Steps (Detailed)

- [x] 1.1: Add `TokenKey` alias, `LifetimeToken` class with `create_token`, `invalidate_token`, `is_ancestor`, `is_claimed_by`. Add `add_to_antichain` helper that maintains antichain invariant (removes dominated tokens) and inheritance arena invariant (releases arenas for removed tokens).
- [x] 1.2: Add `NodeData` and `MemoryStoreNode` types.
- [x] 1.3: Refactor `MemoryStore` internals: replace `StoredNode`/`DerivedNode`/`InheritedNode` with `MemoryStoreNode`. Replace `required_by`/`_dead_pre_cleanup`/`_fresh_nodes` with antichain-based GC. Inline bytes storage (drop `InMemoryBytesBackingStore` / `LMDBBytesBackingStore`). Update `bare_store` to accept a `token` parameter and handle all antichain management internally.
- [x] 1.4: Add token parameter to `MemoryArena` creation. Update `store`, `load`, `unsafe_lazily_attach_from_arena`, `copy_from_arena` (drop same-store fast path; use unified DAG-walk logic), `abandon`, `close`, `copy_to_new_arena` to use tokens and antichains.
- [x] 1.5: Replace `GLOBAL_BACKING_STORE` with `GLOBAL_MEMORY_STORE`. Update `FxtrScope.__init__` to use global store and create a token.
- [x] 1.6: Update `FxtrScope` lifecycle: `__asynccontextmanager__` exit invalidates token (instead of closing store). Simplify `lift` to just `track_in_arena` with claim validation. Update `copy_from_scope` to use unified `copy_from_arena`. Update `store_cbor`.
- [x] 1.7: Add `_detach_token` API (replaces `_detach_store`). Update `fxtr_rpc_local.py` to use `_detach_token` / `invalidate_token` instead of `_detach_store` / `store.close`.
- [x] 1.8: Update `test_memory_arena.py` for new internals. Use fresh global stores (via contextvar override) for memory store unit tests.
- [x] 1.9: Run full test suite; fix any remaining breakage in higher-level tests.
- [x] 1.10: Update CODE_OVERVIEW.md.

---

## Risks and Open Questions

1. **Performance of antichain operations**: Adding/removing tokens from antichains involves set operations and antichain maintenance (removing dominated elements). For most workloads this should be fast since antichains are small (typically 1-2 tokens). But we should profile after the refactor.

2. **Backward compatibility of `copy_to_new_arena`**: Currently this creates a new arena in the same store. In the new system, the forked arena should use the same token as the source arena. Callers (RPC, stream sinks, SQLite pinning) should be checked to ensure this is correct.

3. **Deep fetchability**: The `_deeply_fetchable` mechanism on scopes adds `_deep_fetch_sources` for `ObservableStore` instances. This should continue to work as-is (it's about direct-by-ID lookups bypassing scope tracking, which is orthogonal to the lifetime token system).

4. **Global store test isolation**: For tests that directly test `MemoryStore` internals, create a fresh store and temporarily override `GLOBAL_MEMORY_STORE`. Most other tests can share the default global store — token lifetimes automatically isolate data, and tests may need access to entities from the global scope. If we run into cross-test pollution, we can revisit and isolate specific tests.

5. **`MemoryStore` no longer implements `ObservableStore`**: The global store doesn't fit the `ObservableStore` abstraction (which assumes the store *is* the authority and any caller can look things up). Removed the base class and the unscoped `try_lookup`. The store now only exposes `try_lookup_for_token(node_id, token)` which requires callers to specify their token for visibility checks. `backrefs` is kept as a regular method (not an ABC override).

---

## Implementation Status (in progress)

### Completed
- [x] 1.1: `LifetimeToken`, `TokenKey`, antichain helpers (`_add_to_antichain`, `_is_node_claimed_by`)
- [x] 1.2: `NodeData` and `MemoryStoreNode` unified types
- [x] 1.3: `MemoryStore` internals fully rewritten (antichain-based GC, inline bytes, `bare_store`/`bare_load` take token param)
- [x] 1.4: `MemoryArena` updated (token association, `new_arena(token)` replaces `new_empty_arena()`, unified `copy_from_arena` DAG walk)
- [x] 1.5: `GLOBAL_MEMORY_STORE` contextvar replaces `GLOBAL_BACKING_STORE`; `FxtrScope.__init__` creates token from parent tokens
- [x] 1.6: `FxtrScope` lifecycle: exit invalidates token, `lift` simplified to `_track_from_store`, `_detach_token` added
- [x] 1.7: RPC updated: `_detach_token` replaces `_detach_store`, `_detached_tokens` replaces `_detached_stores`
- [x] 1.8: `test_memory_arena.py` updated (all 55 tests pass)
- [x] 1.10: CODE_OVERVIEW.md updated

### Test results (as of last run)
- `tests/test_memory_arena.py`: **55 passed**
- `tests/test_entity_ref_scope/`: **146 passed**
- Full suite: **1884 passed, 0 failed**

### Fixed issues

**1. Inheritance arena lifecycle bug (fixed)**

`bare_lazily_attach_from_arena` stored the source arena reference directly in `inheritance_arenas` when the node already existed, without making an owned copy. The caller then closed the original source arena, leaving a dangling reference. Fix: always create an owned copy via `copy_to_new_arena` before delegating to `_adopt_inheritance_arena_claim`.

**2. `new_empty_arena` references in deprecated/git_store tests (fixed)**

Updated all test fixtures and inline calls from `store.new_empty_arena()` to `store.create_token()` + `store.new_arena(token)`.

**3. Deep fetch visibility through global store (fixed)**

With the global store, `_deep_fetch_cached` was calling the unscoped `MemoryStore.try_lookup`, which returns any node with an active token — even nodes from unrelated scopes. This broke isolation: an isolated consumer scope could find entities it shouldn't know about, bypassing `_deep_fetch_sources` entirely. Fix: added `MemoryStore.try_lookup_for_token(node_id, token)` which filters by `_is_node_claimed_by`, and changed `_deep_fetch_cached` to use `self._global_store.try_lookup_for_token(node_id, self._token)` instead of the unscoped `try_lookup`.

### Remaining issue: case 2 vs case 3 claim confusion

**Root cause**: The initial implementation used `node.data is not None` as a proxy for "this token can safely access the node." But data can be in memory because a *different* token stored it, without the requesting token having propagated claims to the node's deps. In the old system, this was handled by the `InheritedNode` type — `isinstance(node, InheritedNode)` meant "this scope doesn't own the data yet." The new system collapsed that distinction by checking data presence instead of per-token ownership.

**Example**: `IndexComputer` creates an isolated scope (token T2) and lazily attaches an indexer entity from a MemoryArena (whose scope had token T1, child of global token T0). The indexer's data exists (stored by T1), and its dep (a type entity) has `antichain = {T0}`. T2 is unrelated to T0 (isolated, no ancestors), so the dep is not claimed by T2. When loading the indexer visits its children and tries to `track_in_arena`, the claim check fails.

**Fix**: Separate case 2 and case 3 claims. See step 1.9a below.

---

## Step 1.9a: Case 2/Case 3 Claim Separation

### Design

Replace stored `antichain` field with two separate sets and a computed property:
- `owned_tokens: set[LifetimeToken]` — case 3 claims
- `inheritance_arenas: dict[LifetimeToken, ReadableArena]` — case 2 claims (re-keyed from `TokenKey`)
- `antichain` property = `owned_tokens | set(inheritance_arenas.keys())`

**Key invariants**:
- Antichain property (no two comparable tokens) holds across the full computed antichain.
- `token.nodes_in_antichain` reverse index is maintained for tokens in either set.
- A token in `owned_tokens` means: data is in memory AND all deps are also claimed by this token. This is always established bottom-up (deps before parents), never through a speculative recursive walk.

**`_has_case3_access(node, token) -> bool`**: Returns True if `token` or any ancestor of `token` is in `node.owned_tokens`.

**`_ensure_own` semantics change**: Instead of checking `node.data is not None`, check `_has_case3_access(node, token)`. If the token already has case 3 access, return immediately. Otherwise, **always go through the inheritance arena path** — even if data happens to exist in memory from another token. The inheritance arena is the authority that grants access: loading from it gives case 2 claims on children, which can then be converted to case 3 when those children are loaded in turn.

This mirrors the old `InheritedNode` pattern exactly: the old code checked `isinstance(node, InheritedNode)` and always loaded from the inheritance arena if so, regardless of whether data existed elsewhere. The equivalent check in the new system is `not _has_case3_access(node, token)`.

The "redundant" load when data already exists is cheap:
- For d/n nodes: `bare_store_flat` deduplicates (checks `_already_has_data_for`, just adds owned token).
- For derived nodes: the inheritance arena is typically a MemoryArena (in-memory lookup).
- For `_build_tmp_arena_copying_from_external_arena`: same pattern — check `_has_case3_access` instead of `entity_id in _entity_states`, load from source if not owned.

There is **no `_propagate_case3`** helper. We never grant case 3 by walking in-memory data and claiming deps — that would fail when a dep exists in an external source the token doesn't have access to. Instead, access always flows through the inheritance arena chain.

**`_add_to_antichain` refactored**: Renamed to `_add_owned_token`. Checks domination against the full computed antichain and cleans up dominated tokens from both `owned_tokens` and `inheritance_arenas`. A separate path in `_adopt_inheritance_arena_claim` handles adding to `inheritance_arenas` with the same domination logic.

### Implementation steps

- [x] 1.9a.1: Add `owned_tokens: set[LifetimeToken]` to `MemoryStoreNode`. Make `antichain` a computed property. Add `_has_case3_access` helper. Re-key `inheritance_arenas` from `TokenKey` to `LifetimeToken`.
- [x] 1.9a.2: Refactor `_add_to_antichain` → `_add_owned_token` (case 3), plus `_is_dominated_by_antichain` / `_remove_dominated_from_antichain` helpers. Update `_adopt_inheritance_arena_claim` to use helpers directly for case 2.
- [x] 1.9a.3: Update `bare_store` / `bare_store_flat` / `_build_tmp_arena_copying_from_external_arena` to use `_add_owned_token`.
- [x] 1.9a.4: `_adopt_inheritance_arena_claim` / `bare_lazily_attach_from_arena` remain case 2 only (inheritance_arenas, not owned_tokens).
- [x] 1.9a.5: Rewrite `_ensure_own` and `_ensure_own_derived` to check `_has_case3_access` and always go through inheritance arena when not owned. No `_propagate_case3`.
- [x] 1.9a.6: Update `invalidate_token` and `_free_node` to clean up `owned_tokens`.
- [x] 1.9a.7: Run full test suite; fix any remaining issues.
- [x] 1.9a.8: Update `test_memory_arena.py` if any unit tests need adjustment for the new field.
