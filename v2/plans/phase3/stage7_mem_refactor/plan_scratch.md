# Stage 7 Memory Refactor — Scratch Notes

## Current architecture summary

### MemoryStore (per-scope)
- Each FxtrScope creates its own MemoryStore
- MemoryStore holds `_entity_states: dict[FxtrID, StoredNode | DerivedNode | InheritedNode]`
- Each store has a backing store (GLOBAL_BACKING_STORE contextvar, shared across all stores) for raw bytes
- Store manages arenas (ArenaKey → MemoryArenaState with liveset)
- GC via required_by (set of FxtrID | ArenaKey) on each node; dead_pre_cleanup set

### Node types
- StoredNode: d/n node with CBOR bytes in backing store. Tracks requires, strong_linked_refs, unwraps_to
- DerivedNode: ./!/p/c/u contentless node. Tracks ctx_node, backstep_node, requires, unwraps_to
- InheritedNode: placeholder for data from another arena. Tracks inheritance_parent_arena, unwraps_to

All three share: required_by (set[FxtrID | ArenaKey]), wrapped_by (set[FxtrID])

### MemoryArena
- ReadWriteArena backed by a MemoryStore
- Has an ArenaKey and liveset in the store
- store(): ensures all children tracked, calls bare_store, tracks result
- load(): loads from store, tracks all child IDs in arena
- unsafe_lazily_attach_from_arena(): creates InheritedNode via copy_to_new_arena + _adopt_inheritance_arena_claim
- copy_from_arena(): fast path if same store (just track_in_arena), otherwise _build_tmp_arena_copying_from_external_arena

### FxtrScope
- Creates MemoryStore + main arena + context marker
- Tracks parents, _all_ancestor_scope_ids for inclusion checks
- lift(): walks parent scopes, finds node, bridges via unsafe_lazily_attach_from_arena
- copy_from_scope(): lifts in source, then copy_from_arena
- store(): delegates to entity._fxtr_store_impl via as_active_scope
- load(): lift, then load from arena, deserialize
- Per-scope _ref_loaded_value_cache
- On exit: shutdown keep_alive, close main arena, close store (if not detached)

### External resources
- SQLiteFxtrNamespace creates a FxtrScope, uses unsafe_lazily_attach_from_arena to bridge entities
- GitFxtrNamespace similarly
- RPC uses _detach_store() to keep entity data alive after scope exits
- HTTP client similar pattern

### Key invariants
- Complete subgraph: if you have an ID, you can load it and all its transitive refs
- Parent lifetime ordering: child scopes keep parent scopes alive via keep-alive mechanism
- Arena liveset: loading a node adds all its children to the arena

## New architecture key decisions (from conversation)

1. Single global MemoryStore (replaces per-scope stores)
2. GLOBAL_BACKING_STORE → GLOBAL_MEMORY_STORE; bytes storage inline in store
3. Lifetime tokens: DAG (multiple parents), mirror scope parent structure
4. Parent tokens always outlive child tokens (enforced by scope keep-alive; token invalidation must happen child-first)
5. Node representation: unified MemoryStoreNode with optional NodeData + antichain + inheritance_arenas map
6. Antichain: set of tokens with direct claims (case 2: inheritance arena, or case 3: in-memory + deps claimed)
7. Each token maintains set[FxtrID] of nodes it appears in antichain for (for cleanup)
8. Scope:token is 1:1 in phase 1; arena:token is many:1 (forked arenas)
9. Scopes maintain own parent tracking separately from token hierarchy
10. Store uses just {T} for initial antichain entry (not full intersection)
11. lift: downward only, just add to arena liveset, validate claim exists (case 1)
12. copy_from_scope: async, walks DAG, extends claims
13. track_from_scope: sync, walks DAG, errors on unreachable external resources
14. Per-scope caches remain (not centralized)

## GC model (worked out during conversation)

Token invalidation order is guaranteed (child before parent). So:
- When token T is invalidated: remove T from all antichains, close all arenas with T
- If a node's antichain becomes empty AND it's not in any arena liveset → GC the node
- We still need required_by for node-to-node deps (to keep transitively-needed nodes alive)
  - Actually: with the token ordering guarantee, if T is invalidated, all child tokens are already invalidated.
    So any node that had T in its antichain and no other token can be freely GC'd — its dependents must have
    already lost their T-derived claims when T's children were invalidated.
  - Wait, that's not quite right. A node might have T in its antichain, and another node (with some different
    token T2 where T is NOT an ancestor of T2) might reference it. In that case, that other node's antichain
    entry for T2 would require this node to also be claimed by T2. If T2's claim on this node was via case 1
    (some ancestor of T2 in antichain), and that ancestor is T... but T2 is not a descendant of T, so T
    can't be an ancestor of T2. So T2's claim on this node must be independent of T.
  - So: if a node has {T, T2} in its antichain and T is invalidated, it retains {T2}. GC only when antichain
    is empty.
  - For node-to-node deps: we need to ensure that if A depends on B, and A has {T2} in its antichain, then
    B is also claimed by T2 (or an ancestor). This was verified when T2 was added to A's antichain (case 3).
    So B must have some token in its antichain that is T2 or an ancestor of T2. If that token is later
    invalidated... but it can't be, because it would have to outlive T2 (since it's an ancestor of T2).
  - So: the token ordering invariant guarantees that if A has T2 in its antichain, and B is a dep of A,
    B's claim-grounding token for T2 will outlive T2. Therefore B will always be alive as long as A's
    T2 claim is alive.
  - THEREFORE: we do NOT need required_by for node-to-node deps! The antichain + token ordering invariant
    is sufficient for GC correctness.
  - GC is simply: antichain becomes empty → free the node (including its in-memory data, inheritance arenas,
    backing store bytes)

Wait, but we still need required_by for ArenaKey tracking. Arenas track livesets, and nodes in the arena
liveset need to be kept alive. The liveset is a subset of nodes claimed by the arena's token. When the
arena is closed (or its token invalidated), nodes are removed from the liveset.

Actually, do we need arena livesets at all in the new system? Let me think...

Arena livesets serve several purposes:
1. Determining which nodes a particular arena can access (is_tracked check)
2. Ensuring loaded children are also tracked
3. GC: when arena closes, nodes only in that arena can be freed

In the new system:
1. Access control is handled by the antichain + token hierarchy (is this node claimed by my token?)
2. Children still need to be tracked on load
3. GC is handled by antichain emptying

So arena livesets are still needed for (1) and (2) — the liveset tracks the specific set of nodes
that this particular arena has "opened" for access, which is a subset of all nodes claimed by the
arena's token.

But for GC purposes, we don't need the liveset to participate. When a token is invalidated, we
remove it from all antichains and free nodes with empty antichains. Arena livesets are cleaned up
as a consequence.

Required_by can be simplified to just ArenaKey entries (for liveset tracking / GC from individual
arena closes). We don't need FxtrID entries anymore since the antichain handles transitive lifetime.

Actually, even ArenaKey required_by might not be needed. Currently:
- required_by with ArenaKey: keeps node alive because it's in an arena's liveset
- When arena is closed, ArenaKey is removed from required_by

In the new system, individual arena closure doesn't determine node lifetime — token invalidation does.
When an arena is closed but its token is still active, the node should remain claimable by other
arenas with the same token. So closing an arena doesn't free nodes.

So required_by can be replaced entirely by:
- Antichain (for lifetime/GC)
- Arena livesets (for access control on specific arenas)
- No need for required_by at all!

Actually wait, we still need something to track which arenas a node is in, so that when we close
an arena, we can remove the node from its liveset. But that's already in MemoryArenaState.liveset
(the arena tracks which nodes it contains). We don't need the reverse mapping.

Hmm, but currently `untrack_from_arena` updates both the arena liveset AND the node's required_by.
In the new system, `untrack_from_arena` would just update the arena liveset. And GC happens when
the token is invalidated, not when individual nodes are untracked.

But we might want to support "abandon" (explicitly untracking a node from a scope/arena). In the
current system, abandoning a node can trigger GC. In the new system, abandoning removes the node
from the arena's liveset, but doesn't affect the antichain. The node stays alive (claimed by the
token) even if no arena is currently tracking it. This is fine — it'll be cleaned up when the token
is invalidated.

This is actually a simplification! No more GC on abandon. GC only happens on token invalidation.
And since token invalidation follows the child-before-parent order, we get correct transitive cleanup.

## Phase 1 design: data structures

### LifetimeToken
```python
@dataclasses.dataclass
class LifetimeToken:
    id: int
    parents: frozenset[LifetimeToken]  # direct parents in the DAG
    _all_ancestors: frozenset[int]     # precomputed: IDs of all ancestors (for O(1) is-ancestor checks)
    _nodes_in_antichain: set[FxtrID]   # nodes where this token appears in the antichain
    active: bool = True
```

### MemoryStoreNode (replaces StoredNode/DerivedNode/InheritedNode)
```python
@dataclasses.dataclass
class NodeData:
    """In-memory loaded data for a node."""
    entity_bytes: bytes | None         # CBOR bytes (for d/n nodes)
    requires: frozenset[FxtrID]        # direct deps
    strong_linked_refs: frozenset[FxtrID]  # strong links (for d/n)
    unwraps_to: FxtrID | None
    ctx_node: FxtrID | None            # for c/p nodes
    backstep_node: FxtrID | None       # for p nodes

@dataclasses.dataclass
class MemoryStoreNode:
    my_id: FxtrID
    data: NodeData | None              # None if only known via inheritance
    inheritance_arenas: dict[LifetimeToken, ReadableArena]   # token → external arena
    antichain: set[LifetimeToken]      # direct claims
    wrapped_by: set[FxtrID]            # nodes that unwrap to this one
```

Wait, do we still need wrapped_by? Currently it's used for GC (wrapped_by keeps nodes alive even
without required_by). In the new system, unwraps_to relationships are tracked in NodeData.unwraps_to,
and the wrapping node keeps the unwrapped node alive via its antichain (transitively). So wrapped_by
might not be needed for GC. But it IS used for backreferences (ObservableStore.backrefs_from_store).
Let's keep it for that purpose but not use it for GC.

## Phase 1 design: operations

### create_token(parents)
- Validate all parents are active
- Compute _all_ancestors from parents
- Return new token

### invalidate_token(token)
- Verify no active child tokens exist (check all active tokens to see if any have this as ancestor)
  - Actually, should maintain a set of direct children and check those
- For each node in token._nodes_in_antichain:
  - Remove token from node.antichain
  - If antichain is empty: GC the node
- Mark token inactive

### GC a node
- If data is not None and data.entity_bytes is not None: free from backing store
- For each inheritance arena: abandon(node.my_id) and close if no other refs
  - Actually, inheritance arenas might be shared. Just abandon the node from each.
- Remove from all arenas' livesets (how? need reverse mapping or just skip this)
  - Actually, when token is invalidated, we close all arenas with that token anyway.
  - But other tokens' arenas might have this node. Need to untrack from those.
  - Hmm, this means we need to know which arenas track this node. Back to required_by for ArenaKey?

OK let me reconsider. When token T is invalidated:
1. All arenas with token T should be closed (or at least have their association with T severed)
2. For nodes only claimed by T: GC them. This may require removing them from OTHER arenas' livesets.

Case: node X has antichain {T}. Token T is invalidated. X's antichain becomes empty. But some
arena with token T2 (where T is NOT an ancestor of T2) might have X in its liveset. How did X
get into that arena? It must have been tracked there. But for X to be trackable in a T2-arena,
T2 must have a claim on X. T2's claim on X requires T2 (or an ancestor) to be in X's antichain.
But X only has {T} in its antichain. So T must be an ancestor of T2. But we said T is NOT an
ancestor of T2. Contradiction!

So: a node can only be in an arena's liveset if the arena's token (or one of its ancestors) is in
the node's antichain. Therefore, when the antichain is empty, the node cannot be in any arena's
liveset. We don't need to explicitly remove it from arenas on GC.

Wait, but what about case 1 (parent token has claim)? If X has {T} and arena has token T_child
(where T is ancestor of T_child), then T_child has an implicit claim on X via case 1. So X could
be in T_child's arena. When T is invalidated... but T can't be invalidated while T_child is still
active (child-before-parent). So by the time T is invalidated, T_child is already invalidated, and
T_child's arena is already cleaned up. So X is not in any T_child arena when T is invalidated.

So the invariant holds: when we invalidate T and a node's antichain becomes empty, it's guaranteed
to not be in any active arena's liveset. We can just free the node without worrying about arenas.

This means we DON'T need required_by at all. GC is purely antichain-based, and the token ordering
invariant ensures correctness.

One remaining question: within a single token's lifetime, can we have nodes that are in memory but
not in any arena's liveset? Yes: a node that was stored (put into memory store) but then its arena
was closed (or the node was abandoned from the arena). The node still has the token in its antichain,
so it's alive. It'll be freed when the token is invalidated.

This is a minor memory concern (nodes lingering after abandon until token invalidation), but it's
simple and correct. We could optimize later with eager cleanup, but for now it's fine.

## Implementation plan outline

### Phase 1: Global store + lifetime tokens

Step 1: LifetimeToken dataclass + basic operations (create, invalidate, is_ancestor)
Step 2: MemoryStoreNode unified type + NodeData
Step 3: Refactor MemoryStore to be a singleton-style global store with token-based lifetime
Step 4: Refactor MemoryArena to use tokens instead of per-scope stores
Step 5: Refactor FxtrScope to use global store + tokens
Step 6: Update external resource integration (SQLite, Git, RPC, HTTP)
Step 7: Update tests

### Phase 2: track_from_scope

Step 1: Implement track_from_scope on MemoryStore/MemoryArena
Step 2: Expose as FxtrScope.track_from_scope
Step 3: Tests

### Phase 3: Token sharing + weak scopes

Step 1: Allow scopes to share parent's token when conditions met
Step 2: Implement weak scopes
Step 3: Tests
