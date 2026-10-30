# Stage 9 / Stage 2 post1 — fix memory-store sync-dep invariant

## Background — what Stage 2 patched

Stage 2 surfaced (and worked around) a latent bug in
``MemoryStore._bare_store_impl``'s n-node dedup path: when a token
re-stores an already-existing n-node, the code only added the new
token to the n-node's ``owned_tokens`` and skipped the recursive
"store the unwrap form" walk. The result was that the new token
gained access to the n-node but not the d-node it unwraps to —
breaking the post-store ``unwrap_provenance`` reachability check.
Stage 2 patched this by walking the n-node's ``unwraps_to`` chain
and calling ``_add_owned_token`` along the way (see the ``# TODO:
Fix this more comprehensively...`` comment in ``_bare_store_impl``).

That patch is one symptom of a broader invariant violation in the
post-refactor memory store. This stage states the invariant
cleanly, audits the gain-of-access sites against it, and routes
every site through a primitive that maintains it by construction.

## The invariant

The arena protocol exposes four sync queries that read
relationships off a node:

| Query | Returns | When the source can answer |
|---|---|---|
| `unwrap_provenance(N)` | `N.unwraps_to` | always, for any N in the source |
| `c_node_to_context_data(N)` | `N.data.ctx_node` | always, for any c-node |
| `p_node_to_pathroot(N)` | `N.data.ctx_node` | always, for any p-node |
| `step_back_p_ctx(N)` | `N.data.backstep_node` | always, for any p-node |

We call the relations these queries return the **sync-discoverable
deps** of N. They share the property that the source arena can
answer them synchronously without async I/O — so a token gaining
access to N can also be granted access to N's sync-discoverable
deps without leaving sync land.

(In contrast, ``data.requires`` for d/n nodes lists the full set
of CBOR children, which is *not* sync-discoverable: there is no
arena method that returns it without an async ``load_flat``. So
``requires`` is a load-time dep, granted lazily during load,
under a separate "the load propagates access along the way"
contract.)

The invariant we maintain is then simply:

> **Sync-Dep Closure.** For every token T and every node N
> currently in the store: if T has access to N (owned or
> inherited), then T has access to every sync-discoverable dep of
> N — recursively.

The recursive closure is shallow in practice: each node has at
most three sync deps (`unwraps_to`, `ctx_node`, `backstep_node`),
each dep is itself a node with its own (shallower) sync deps, and
the chain terminates at d-nodes (no sync deps), built-ins (no
sync deps), or contentless leaves like u-node→d-node→done.

### Why this is enough

The invariant is exactly strong enough to make every public op
work locally:

- **Sync queries** (`unwrap_provenance`, `c_node_to_context_data`,
  `p_node_to_pathroot`, `step_back_p_ctx`): pre-condition is "T
  has access to N." Op returns a sync-dep ID. By the invariant, T
  has access to that ID. The arena layer's post-call
  ``track_in_arena`` succeeds. ✓

- **Async loads** (`load`, `load_flat`): visit the loaded CBOR's
  children; grant T access to each via
  ``_adopt_inheritance_entry`` (foreign source) or
  ``_ensure_own`` (sentinel). Each grant is itself a
  gain-of-access site that maintains the invariant on the child.
  d/n's ``requires`` is the load-time-dep half mentioned above.

- **Stores / creates / lifts / track-from / dest-adopt**: each
  grants T access to a node N. By construction (see "Where to
  enforce" below), the grant cascades through sync-discoverable
  deps, so the invariant holds post-call.

- **Token / arena teardown**: the access entries are removed in
  bulk; the invariant is over per-token access state, so removing
  T's entries from every node consistently preserves it.

### Bound on the eager cascade

For each node tag, the maximum sync-dep fan-out at the node is:

- ``.``, ``!``, ``d``: 0 sync deps.
- ``u``: 1 (`unwraps_to`).
- ``n``: 1 (`unwraps_to`).
- ``c``: 2 (`unwraps_to`, `ctx_node`).
- ``p``: 3 (`unwraps_to`, `ctx_node`, `backstep_node`).

Recursing through the deps:

- ``unwraps_to`` chains: bounded by the unwrap depth (n→d, c→d,
  u→d, p→whatever-the-pathroot-value-is). Always terminates at
  a d/builtin in one or two steps for the common cases.
- ``ctx_node`` for c/p: always a d-node (asserted in
  ``create_c_node`` / ``create_p_node`` and at store time). One
  step, then done.
- ``backstep_node`` chains for p: **length 1**. A p-node has a
  backstep iff it was created as the ``$fxtr.ctx`` marker of a
  parent p-node (`_bare_load_impl`'s p-projector sets
  `backstep_node` only for `RawFxtrEntityMarkerID` children).
  Marker-position p-children always have an `unwraps_to` that's
  a contentless tag (`.`, `c`, `p`, `u`) — because `$fxtr.ctx`
  markers always point to context nodes, never d-nodes. Loading
  a p-node with a contentless `unwraps_to` raises ("Cannot load
  a contentless ID"), so backstep-having p-children never spawn
  their own children, and therefore never become the parent of
  another backstep-having p-child. The chain terminates after
  one step.

So the eager cascade per gain-of-access is O(unwrap depth + 1)
sync queries — small and bounded.

## Where to enforce: two primitives

Every token gain-of-access flows through one of two internal
primitives. They have very different shapes — one verifies, the
other constructs.

### Why this asymmetry matters

A natural-but-wrong implementation of the ownership-grant primitive
would do a top-down cascade: "I'm granting T ownership of N, so let
me also walk N's deps and claim ownership of each in turn." That's
**inventing** ownership claims rather than proving them. If a caller
mistakenly invokes the grant on a node whose deps aren't actually
accessible to T, a top-down cascade silently propagates the bad
claim down the chain — T ends up "owning" a tree of nodes it can't
legitimately reach. Future loads or sync queries would then either
fail unexpectedly or, worse, succeed using state that was never
verified.

Ownership of a node has to be **proven from below**: T can own N
only if T can already reach every dep of N. The proof is built
bottom-up by the caller (recursive sub-store, source queries,
worklist deps-first ordering, etc.), and the grant primitive just
records the claim *after* verifying the precondition.

Source-driven adoption is the dual case: there the proof comes
from the source itself, since each sync query against the source
both returns a dep ID *and* auto-tracks it in the source's liveset.
The cascade in `_adopt_inheritance_entry` is therefore
proof-constructing, not claim-inventing — every recursive step
issues a query whose successful return is the proof that we
actually have the dep.

### The two primitives

1. **Direct ownership grant — strict assertion.**
   ``_add_owned_token(N, T)``. Used when the caller already has
   the data locally (fresh stores, store dedup, ownership
   upgrades from inheritance). The function:

   - Asserts ``node.data`` is populated (you can't own a node
     whose data isn't loaded — there'd be no way to prove dep
     coverage).
   - Asserts T has access (owned or inherited) to
     ``node.unwraps_to`` (if not None) — the sync-discoverable
     unwrap.
   - Asserts T has access to every ID in ``node.data.requires``
     — covers sync-deps for c/p (ctx_node, backstep_node) *and*
     load-time deps for d/n (CBOR children).
   - If all checks pass, adds T to ``node.owned_tokens``.

   No cascade. No fixing-up. The function does not establish
   access to deps; the caller does, before invoking. If the
   assertion fires, an upstream caller violated the contract,
   and we want to know that loudly at the grant site rather
   than later at load time.

   This is a strictly stronger invariant than just sync-dep
   closure — by checking ``data.requires`` we verify load-time
   deps too, so if the new owner's arena later issues a load,
   every CBOR child is already trackable.

2. **Source-driven adoption — proof construction via source queries.**
   ``_adopt_inheritance_entry(N, source, T, T_lease)``. Used
   when a token is gaining access from another arena (lazy
   attach, dest-adopt, foreign loads). The flow is the same
   whether the node is new to the store or already present:

   1. **Discover** every sync-discoverable dep from
      ``source`` via sync queries. For foreign sources, each
      query (``source.unwrap_provenance`` /
      ``c_node_to_context_data`` / ``p_node_to_pathroot`` /
      ``step_back_p_ctx``) auto-tracks its result in the
      source's liveset, which is precisely the proof that
      the source has the dep accessible — *we never install
      a claim we don't actually have*. For sentinel sources
      the same info is read from ``_entity_states``.
   2. **Cascade** through each sync-dep recursively under the
      same ``source``. The recursive query against the
      now-tracked dep grows the source liveset further, so
      the cascade naturally covers the whole closure without
      any upfront closure computation.
   3. **Install** T's claim with shape determined by what we
      learned during discovery (see next section).

### Caller responsibilities

Every other site that grants access — ``_bare_store_impl``
(fresh + dedup), ``_bare_store_flat_impl`` (fresh + dedup),
``create_c_node`` / ``create_p_node`` / ``_create_u_node_impl``,
``track_builtin``, ``_dest_adopt_inheritance``, ``_ensure_own``,
the p-load projector — ends with one of the two primitives.
Each caller is responsible for satisfying ``_add_owned_token``'s
preconditions before invoking it:

- **Fresh stores** establish dep access by recursively
  sub-storing the unwrap form (which itself bottoms out at deps
  that are either trivial or already in the arena's liveset
  per ``_ensure_all_accessible_for_storing``).
- **Inheritance-grant call sites** rely on
  ``_adopt_inheritance_entry``'s cascade having already
  installed inheritance entries on every sync-dep before the
  ownership upgrade runs.
- **n-node dedup in ``_bare_store_impl``** explicitly walks
  the ``unwraps_to`` chain bottom-up (a length-2 chain ``n →
  d``): grant ownership of d-form first (its strict assert is
  empty: d-nodes have no unwraps_to, and its CBOR children are
  reachable via Sync-Dep Closure on the toplevel's provenance
  children), then grant ownership of n-node (its
  ``unwraps_to`` was just gained).
- **Worklist-driven flows** (``bare_copy_from_arena``,
  ``track_from_memory_arena``) rely on LIFO ordering: deps are
  saved before their parents, so by the time the parent's
  grant runs, all deps are already owned-or-inherited under
  the destination token.

If a caller path doesn't fit one of these patterns, the strict
assert in ``_add_owned_token`` will fail loudly at the grant
site — the goal is to make contract violations into immediate
errors rather than latent corruptions.

### Adoption installs ownership for derived, inheritance for d/n

When the discovery step in ``_adopt_inheritance_entry`` returns
a fully-populated ``NodeData`` (any derived tag — ``./!/c/p/u``)
the source has handed over everything sync-readable about the
node. There is *no* future async load that could ever produce
more for this node, so an inheritance entry pointing back at
the source would be dead weight. The clean shape is to install
T as an **owned** token on the node and abandon the source's
claim on it.

For d/n nodes the discovery returns ``NodeData = None`` — the
entity bytes are not sync-discoverable and still need an async
load. T gets an **inheritance** entry pointing at the source so
future loads flow through the bridge.

Concretely, the discovery step issues:

- For `n` / `u`: ``source.unwrap_provenance(N)`` → unwraps_to.
- For `c`: also ``source.c_node_to_context_data(N)`` → ctx_node.
- For `p`: also ``source.p_node_to_pathroot(N)`` → ctx_node and
  ``source.step_back_p_ctx(N)`` → backstep_node.
- For `d`: ``source.unwrap_provenance(N)`` (returns None, no
  more queries).
- For `.` / `!`: trivial empty NodeData; no queries needed.

After this, every derived node adopted by T has T in its
``owned_tokens`` and a fully-populated ``data``. Every d/n node
adopted has T in its ``inheritance_bridges`` and ``data = None``
until an async load fills the bytes.

### `_ensure_own_derived` is gone

Once adoption installs ownership directly for derived nodes
(rather than inheritance plus a deferred upgrade), the old
``_ensure_own_derived`` helper becomes a no-op:

- Its "materialize data via source queries" job moved into
  ``_adopt_inheritance_entry``'s discovery step.
- Its "upgrade T from inheritance to ownership" job is moot —
  derived nodes accessible to T are already owned by T.

The function was therefore removed entirely. Call sites that
needed an access check (the public sync methods
``c_node_to_context_data`` / ``p_node_to_pathroot`` /
``step_back_p_ctx``) now inline an ``_node_has_access`` check
and read ``node.data`` directly. The one site in
``_bare_store_impl``'s p-case that was calling
``_ensure_own_derived(new_id, target_y, …)`` to "materialize
data under target_y" is similarly simplified — by the
invariant, target_y's access already implies populated data, so
the call was a tautology.

### Fork lifecycle for foreign sources

When ``_adopt_inheritance_entry`` is given a foreign source, it
issues source queries that auto-track in the source's liveset,
abandons the source's claim on each derived node it adopts, and
leaves only the d/n nodes (the ones that got inheritance
entries) tracked. The fork's lifetime is then determined
naturally by whether anything still references it:

- Each inheritance entry holds the fork via ``_InheritanceEntry.source``.
- ``MemoryArena``'s ``weakref.finalize`` closes a fork whose
  Python references all drop.
- ``CapIRArena`` doesn't have ``weakref.finalize``, so to support
  the same "abandon what you don't need; cleanup is automatic"
  contract, ``CapIRArena.abandon`` self-closes when it empties
  the liveset (releasing the remote lease and bridge state). An
  empty ``ReadableArena`` is conceptually closed anyway — the
  public surface all checks the liveset before doing anything,
  and there is no public method to add nodes back.

Callers that fork a foreign source (``bare_lazily_attach_from_arena``
foreign mode, ``_dest_adopt_inheritance``) therefore don't need
explicit close logic — the cascade either keeps the fork alive
through inheritance entries or abandons it to empty.

## Audit through the invariant lens

Every gain-of-access site routes through one of the two
primitives. ``_add_owned_token``'s strict assert verifies that T
has access to all deps (sync + load-time) before recording
ownership; each site below explains how it establishes that
access first.

| Site | How the precondition is established |
|---|---|
| `_bare_store_impl` (fresh d/n/p/u) | Recursive sub-store grants T ownership of the unwrap target before the parent's grant runs; arena layer's `_ensure_all_accessible_for_storing` covers CBOR children |
| `_bare_store_impl` (d-node dedup) | d has no `unwraps_to`; `data.requires` (CBOR children) accessible per arena precondition |
| `_bare_store_impl` (n-node dedup) | Explicit `unwraps_to` chain walk: grant ownership of d-form first (deps reachable via Sync-Dep Closure on the toplevel's provenance children), then grant ownership of n-node |
| `_bare_store_impl` (p-node) | Recursive sub-store of unwrap_data + `_adopt_inheritance_entry`'s prior cascade through ctx_node / backstep_node |
| `_bare_store_impl` (u-node) | Recursive sub-store of unwrap_data |
| `_bare_store_flat_impl` (fresh + dedup) | Caller's contract: deps must be in store *and* T must already have access. Worklist-driven callers (`bare_copy_from_arena`, `track_from_memory_arena`) enforce LIFO deps-first ordering; direct callers must obey the contract or hit the assert |
| `create_c_node` / `create_p_node` | Recursive sub-store of context_data establishes ctx_node access; arena layer's `_ensure_all_accessible_for_storing` covers `value.inner_id` |
| `_create_u_node_impl` | Caller (arena layer) had `wrapped_id` in arena liveset |
| `track_builtin` | No deps |
| `_bare_load_impl` p-child creation | Each child carries `requires={ctx_node, parent_p}`; both already in T's claims (parent_p was being loaded under T; ctx_node was its own dep) |
| `_ensure_own` d/n sentinel | After recursing under Y, walks `target_node.data.requires` and adopts each as inheritance under T; `unwraps_to` was inherited at original adoption time |
| `_ensure_own` d/n foreign | Walks all flat-data deps via `_adopt_inheritance_entry` before calling `_bare_store_flat_impl` (which calls `_add_owned_token`) |
| `_ensure_own` derived | T has inheritance on N → Sync-Dep Closure invariant guarantees access to all sync-deps; for derived nodes `data.requires` *is* the sync-dep set |
| `bare_lazily_attach_from_arena` / `_dest_adopt_inheritance` | Routes through `_adopt_inheritance_entry`; the cascade builds proof bottom-up via source queries (auto-tracking) before installing claims |
| `_bare_track_from_memory_arena_impl` save | LIFO ordering: deps are saved bottom-up, so each parent's grant sees deps already covered |

## Concrete bugs that this fixes

1. **n-node dedup unwrap closure** (the original Stage-2 bug):
   ``_bare_store_impl``'s n-node dedup added the new token to
   the n-node's ``owned_tokens`` only, leaving the d-node
   unreachable to that token. Fixed by walking the
   ``unwraps_to`` chain bottom-up — gain ownership of d-form
   first (its `_add_owned_token` strict assert is satisfied
   because d-nodes have no unwraps_to and the d-form's CBOR
   children are reachable via Sync-Dep Closure on the
   toplevel's provenance children), then of the n-node.

2. **Inheritance adoption ctx/backstep gap**: a token inheriting
   a c-node ended up with inheritance on the c-node and
   `unwraps_to` only, not on `ctx_node`. ``is_tracked`` on
   `ctx_node` gave the wrong answer until a sync read
   self-healed. Fixed by routing both new and existing-node
   adoption through the unified discover/cascade flow that
   walks all sync-deps.

3. **`_dest_adopt_inheritance` cross-function contract**: the
   helper used to install only a toplevel inheritance entry and
   relied on its caller's worklist to walk deps separately. With
   the helper now routing through ``_adopt_inheritance_entry``
   directly, the cascade is local.

4. **Source-fork liveset growth for existing nodes** (latent
   pre-existing): when ``_adopt_inheritance_entry`` cascaded for
   an *existing* node via foreign source, it didn't issue source
   queries (it just read from the in-store node's data). The
   forked source's liveset therefore didn't grow to cover the
   sync-dep chain, and a load of a not-yet-loaded sync-dep would
   fail. The unified flow always issues source queries
   (regardless of new vs. existing), so the fork's liveset is
   always in sync with the cascade.

5. **Class of "silently-wrong ownership" bugs**: any future code
   path that grants T ownership of a node without first ensuring
   T's access to deps will assert-fail at the grant site
   (``_add_owned_token``) instead of silently propagating an
   incorrect claim that would cause obscure load failures
   downstream. The strict assertion is the structural safety
   net.

## Implementation steps

1. [x] Add the Sync-Dep Closure invariant to ``MemoryStore``'s
       class docstring; list the sync-discoverable deps explicitly.
2. [x] Add a comment near ``MemoryStoreNode.backstep_node``
       documenting the length-1-chain property (and pointing at
       the p-projector in ``_bare_load_impl`` for the proof).
3. [x] Make ``_add_owned_token`` a **strict assertion**, not a
       cascade. Asserts T has access (owned-or-inherited) to
       ``node.unwraps_to`` and to every ID in ``node.data.requires``
       (covers sync-deps for c/p plus load-time CBOR children for
       d/n). Does not establish access to deps — the caller is
       responsible for proving the precondition holds before
       invoking. The earlier cascade-driven version was
       claim-inventing; the strict version turns precondition
       violations into immediate, loud asserts at the grant site
       rather than latent corruption that surfaces at load time.
       The n-node dedup case (the original Stage-2 bug) is fixed
       by walking the ``unwraps_to`` chain bottom-up explicitly
       in ``_bare_store_impl``'s c-case; see step 8.
4. [x] Add ``_discover_sync_deps_from_source(node_id, source, ...)``
       returning ``(unwraps_to, data)``. For foreign sources
       issues live arena queries (``unwrap_provenance`` plus
       ``c_node_to_context_data`` / ``p_node_to_pathroot`` /
       ``step_back_p_ctx`` for c/p), each of which auto-tracks
       its result in the source's liveset. For sentinel sources
       reads the same info from ``_entity_states``.
5. [x] Unify ``_adopt_inheritance_entry`` around the discover →
       cascade → install flow. The same code path handles new
       nodes and existing-with-data; the difference is just
       whether we create a new ``MemoryStoreNode`` or attach the
       claim to an existing one.
6. [x] Install derived-vs-d/n claims asymmetrically in
       ``_adopt_inheritance_entry``'s install step. Derived
       nodes (data populated by discovery) get T in
       ``owned_tokens`` directly; for foreign sources we also
       abandon the source's claim on the node. d/n nodes get T
       in ``inheritance_bridges`` so future async loads can flow
       through the bridge.
7. [x] Remove ``_ensure_own_derived`` entirely. Internal call
       sites (``_ensure_own`` derived branch, ``_bare_store_impl``
       p-case, ``_bare_track_from_memory_arena_impl`` data path)
       are inlined as direct ``_add_owned_token`` calls or
       ``assert node.data is not None`` checks. Public sync
       methods (``c_node_to_context_data`` /
       ``p_node_to_pathroot`` / ``step_back_p_ctx``) inline a
       ``_node_has_access`` check and read directly. The whole
       "ownership upgrade for derived" path was tautological
       once derived data is always populated.
8. [x] Replace the inline Stage 2 ``while cur is not None`` loop
       in ``_bare_store_impl``'s n-node dedup with an explicit
       bottom-up chain walk: collect the ``unwraps_to`` chain
       (length 2 for n-nodes — n → d), then call
       ``_add_owned_token`` on each level deepest-first. At the
       d-form level the strict assert checks d-form's CBOR
       children are accessible to T (true via Sync-Dep Closure
       on the toplevel's provenance children); at the n-node
       level it checks ``unwraps_to=d-form`` (just gained). The
       ``# TODO: Fix this more comprehensively`` comment is gone.
9. [x] Route ``_dest_adopt_inheritance`` through
       ``_adopt_inheritance_entry`` (option (a) — fork over
       ``[node_id]``, hand off to the unified primitive). The
       cascade grows the fork liveset naturally via source
       queries; no separate worklist deps-push is needed.
10. [x] Self-close ``CapIRArena`` on ``abandon`` to empty.
        Releases ``_remote_lease`` and ``_bridge_state`` when
        the last tracked node is dropped. Lets callers rely on
        "abandon what you don't need; cleanup is automatic"
        without explicit close logic. Also makes
        ``CapIRArena.is_tracked`` return False on closed
        (matching ``MemoryArena``).
11. [x] Add concise local comments at load-bearing sites
        (d-node dedup, n-node dedup, flat dedup,
        ``_dest_adopt_inheritance`` docstring) noting the cascade.
12. [x] Tests, see below.
13. [x] Update CODE_OVERVIEW.md: added Sync-Dep Closure
        invariant to ``memory_arena.py`` section.

## Tests

All tests live under ``tests/test_memory_arena.py`` (with cross-
references from any provenance flow tests). Each is a small
multi-token scenario; all should fail before the fix and pass
after.

a. **Copy-from-arena n-node unwrap reachability** — exercises
   the worklist-driven copy path's analog of the original
   Stage-2 n-node dedup bug. T1 stores an n-node. T2 calls
   ``bare_copy_from_arena`` (the realistic high-level flow that
   ends in ``_bare_store_flat_impl`` with deps-first ordering).
   Assert that post-copy, both n-node and its unwrap target are
   reachable to T2. (A direct ``bare_store_flat`` call without
   prior dep establishment would be a contract violation under
   the strict-assert model and is no longer a meaningful test.)

b. **Inheritance attach of c-node with populated data** —
   T1 creates a c-node (data populated). T2 inherits via
   ``lazily_attach_from_arena``. Assert T2 has access to the
   c-node, its `ctx_node`, and its `unwraps_to` *before* any
   sync read (i.e., `is_tracked` succeeds on each).

c. **Inheritance attach of p-node with populated data** — same
   for p-nodes; assert T2 has access to `ctx_node`, `unwraps_to`,
   and `backstep_node` if present.

d. **Track-from-memory-arena across c-node** — T1 stores c-node
   + d-form. T2 calls ``track_from_memory_arena``. Assert T2
   has access to all sync-discoverable deps without going
   through a sync-read self-heal.

e. **n-node dedup regression** — Stage 2's original repro. T1
   stores n-node, T2 re-stores via ``bare_store``. Assert post-
   store sync-dep access. (Same test, kept as regression for the
   refactor.)

f. **Idempotency** — granting access to a node T already owns
   (or already inherits from the same source) is a no-op. No
   duplicate `owned_tokens` entries, no extra bridge claims.

g. **Diamond shape** — node N has `data.ctx_node = X` and
   `unwraps_to = Y`, with X itself unwrapping to Y. T2 gains
   access to N; assert T2 ends with exactly one access entry
   on Y (the cascade dedups).

h. **p-node `backstep_node` is set only in marker positions** —
   regression test on the projector's invariant that the
   cascade-bound argument relies on. Construct a multi-level
   p-node DAG: pathroot ``D0 = {"value": ref(D1), "$fxtr.ctx":
   "."}`` where ``D1 = {"x": ref(X), "$fxtr.ctx": "."}``;
   ``create_p_node(D0)`` → P0; load P0 to materialize children
   (B0 from the marker slot, Cv from the ``"value"`` slot);
   load Cv to materialize grandchildren (B_cv from its marker,
   Cvx from its ``"x"``). Assert across the materialized p-set
   that ``backstep_node is not None`` exactly for B0 and B_cv,
   and ``backstep_node is None`` for P0, Cv, Cvx. This locks
   the property that backstep chains have length 1; the
   bounded-cascade argument follows from it directly. The
   cascade itself is exercised by tests (c), (d), (g).

## Follow-ups

- **Add ``weakref.finalize`` to ``CapIRArena``** for symmetry
  with ``MemoryArena``'s self-cleanup-on-drop. Today
  ``CapIRArena`` self-closes on abandon-to-empty (covers the
  cascade case) but doesn't auto-close on plain
  reference-drop, so any caller that drops a non-empty
  ``CapIRArena`` without calling ``close()`` still leaks the
  remote lease. ``MemoryArena`` already handles this via
  ``weakref.finalize``; doing the same for ``CapIRArena`` would
  let us reason about arena lifetimes uniformly across impls.
