# Stage 8b — Lease-backed memory store, arenas, and scopes

Rewrite the memory store / memory arena / scope system so that all lifetime
management is expressed in terms of the new lease primitives from
`fxtr2/leases/`. The user-facing scope API stays roughly the same; everything
below it is rebuilt.

> **Subsequent rollback.** Chunk 2 below over-applied the lease-first
> convention to namespace-backend methods (`PersistentPinningBackend` /
> `IndexablePinningBackend` / `DeepFetchable`). Arenas are still lease-gated;
> backend methods are not. See
> [`ns_boilerplate_rollback.md`](ns_boilerplate_rollback.md) for the
> narrowed contract, the specific changes, and what was kept.
> The chunk-2 checklist items remain checked off here as a historical
> record of what was shipped before the rollback.

See `sketch.md` for the original goals. This doc fleshes them out into a design
and a concrete implementation checklist.

## High-level picture

- The memory store no longer has its own token type. Internally it allocates
    `UnconditionalLocalLifetime`s (one per token), registers an `end_callback`
    on each for GC/cleanup, and hands callers a clone of the ULL's root lease.
- Every arena (ABC `ReadableArena[B]`) is parameterized on a lifetime brand and
    exposes a `token` property. Every method takes a `Lease[B]` as its first
    argument. The arena validates grant identity on each call and consumes the
    lease (standard `.take()` pattern).
- A `FxtrScope` owns exactly one arena *and* one lease clone for that arena's
    token (the arena's "main lease"). Scope exit discards the lease and closes
    the arena. Keep-alive, weak child scopes, and parent drain ordering are
    unchanged — orthogonal to lease-backed memory-store GC. The backing token
    dies on its own schedule once no lease clones remain anywhere in the system.
- Mounting an external arena into the memory store becomes safe: callers must
    supply a key lease (naming matches `AttachedLocalLifetime`) and a converter
    that produces a fresh source-arena lease whenever the store needs to
    access the attached node.

Lifetime ordering is enforced structurally by the lease DAG (store holds a
clone of each parent lease keyed by the child token) rather than by scope
keep-alive bookkeeping.

## Core data structures

### `MemoryStore` internal state

```
_token_data: dict[LifetimeToken, LifetimeTokenData]
    # Per token: ``ull``, ``grant``, ``nodes_in_antichain``, ``parents``,
    # ``parent_leases``, ``ancestor_leases`` (strict ancestors only; no self-key),
    # ``foreign_leases`` (maps Y → ForeignLeaseData). The ULL lives here; root
    # leases were taken and cloned out at ``create_token_from_leases``.

_entity_states: dict[FxtrID, MemoryStoreNode]

_bytes_data / _bytes_refcounts
    # Inline deduplicated storage for node CBOR payloads.

_arena_counter, _arenas: dict[ArenaKey, MemoryArenaState]
    # Each open arena: token + liveset (``MemoryArena`` reads liveset via this row).
```

**`LifetimeTokenData`** (the value type for `_token_data`): one struct per token the
store issued. Created in `create_token_from_leases`; `foreign_leases` starts empty and
grows when this token registers foreign arenas as `X_reg`.

- **`ull`** — `UnconditionalLocalLifetime` owned by the store; the abstract token is
    `ull.token`. Root lease is not stored separately on the row: at creation the
    store takes `ull.root_lease`, returns a clone to the caller, and discards its own
    handle so the returned clone (and later clones) hold the lifetime open.
- **`grant`** — cached expected grant for `validate_lease_grant` (same as
    `ull.root_lease.grant`).
- **`nodes_in_antichain`** — reverse index: every `FxtrID` whose antichain (case-2 ∪
    case-3 tokens) includes this token, so `_on_token_end` can find affected nodes
    quickly.
- **`parents`** — direct parent tokens in the lifetime DAG, in parent-list order.
- **`parent_leases`** — one held lease clone per **direct** parent; keeps each parent
    alive at least as long as this child token. Cleared when the child ends.
- **`ancestor_leases`** — one held lease clone per **strict** ancestor (transitive
    closure of parents). Never contains this token as a key (would cycle). Used for
    O(1) “is ancestor?” checks and to obtain a fresh `Lease[X_reg]` for the
    converter’s `base` when the caller is a strict descendant of `X_reg`.
- **`foreign_leases`** — maps foreign arena token `Y` → `ForeignLeaseData` for every
    `(X_reg, Y)` bridge where this token is `X_reg` (key lease, converter, claimed
    IDs, break callback). Source arena objects live on nodes’ `inheritance_claims`,
    not here.

`_token_data[X_reg].foreign_leases[Y]` is a deduplicated record keyed by the *pair* of
tokens `(X_reg, Y)` where `X_reg` is the memory-store token that *registered*
the foreign arena (via `lazily_attach_from_arena`) and `Y` is the foreign
arena's own token. The source arena itself is **not** stored here —
multiple source arenas may share the same `Y` token but hold different
per-arena tracking state, so they aren't fungible. The key lease and
converter, by contrast, are fungible per `(X_reg, Y)`.

Descendants of `X_reg` access the arena through ancestry (the same rule as
today's `_pick_inheritance_arena`: Y must be ancestor-of-or-equal-to the
caller's token). The `Lease[X_reg]` that the converter consumes as its
`base` argument is not stored inside the `(X_reg, Y)` record — see
"`base` lease sourcing" below.

```
@dataclass
class ForeignLeaseData:
    key_lease: Lease[Z]                     # store owns this clone
    converter: LifetimeConverter[B_X, B_Y, B_Key]  # (Lease[B_Key], Lease[B_X]) -> Lease[B_Y]
    claimed_ids: set[FxtrID]                # nodes backed by any Y-arena
    deregister_break_callback: Callable[[], None]  # call before discarding key_lease
    break_reason: BaseException | None      # set when key_lease breaks
```

When `claimed_ids` becomes empty, the store discards `key_lease`, detaches
the break callback, and removes the entry. When the key lease's
broken-callback fires, the store:
1. Sets `break_reason` to the received exception.
2. Emits `logging.error` naming the `X_reg` token and the list of affected
    IDs (they are now unloadable).
3. Leaves node entries in place — subsequent loads surface the stored
    `break_reason` (see `bare_load` flow below).

### `base` lease sourcing (no-cycle rule)

When the store calls `converter(key, base)`, `base` must be a fresh
`Lease[X_reg]`. We **never** hold a clone of `X_reg`'s own lease in data
keyed on `X_reg` (doing so would prevent `X_reg` from draining — the store
would be holding its own token alive). Instead:

- If the caller's token `X'` **equals** `X_reg`, clone the caller's own
    lease (the one passed into `bare_load`) to produce `base`.
- If `X'` is a strict descendant of `X_reg`, clone
    `_token_data[X'].ancestor_leases[X_reg]` to produce `base`. The ancestors map is
    populated at token-creation time by cloning each parent's own
    `_ancestor_leases` entries plus the parent itself.

Either way the lease used as `base` is owned by someone other than
`X_reg` itself, so `X_reg` can still close cleanly.

### `MemoryStoreNode` (revised)

```
@dataclass
class MemoryStoreNode:
    my_id: FxtrID
    data: NodeData | None
    unwraps_to: FxtrID | None
    owned_tokens: set[LifetimeToken]
        # case 3: data + deps claimed
    inheritance_claims: dict[LifetimeToken_X_reg, ReadableArena[Y]]
        # case 2: X_reg (the registering token) claims via source arena.
        # Y is inferred from arena.token. The (key_lease, converter) for
        # (X_reg, Y) live in store._token_data[X_reg].foreign_leases[Y]; arena is
        # stored here because different source arenas with the same Y are
        # not fungible. Descendants of X_reg see this claim via ancestry
        # (matches current _pick_inheritance_arena semantics).
    wrapped_by: set[FxtrID]
```

`antichain(node) = node.owned_tokens ∪ node.inheritance_claims.keys()`.
Node is freed when the antichain becomes empty.

### `LifetimeConverter` protocol

```
class LifetimeConverter(Protocol[B_X, B_Y, B_Key]):
    def __call__(self, key: Lease[B_Key], base: Lease[B_X]) -> Lease[B_Y]:
        ...
```

Consumes both input leases, returns a fresh `Lease[Y]` each call. Default
converter (`default_lifetime_converter`, used when key and foreign arena share a brand): discards `base`, returns `key` unchanged.
Naming mirrors `AttachedLocalLifetime.exchange_forward(key_lease, req_lease)`,
which is the canonical non-default implementation.

## `MemoryStore` public API

All token-taking methods are renamed to lease-taking. Ownership convention:
every lease passed in is consumed (`.take()` style); the store clones
internally when it needs to persist one. **Calling convention: each lease
argument appears immediately after the thing it authorizes.** For a
single-target method, the arena/store's own lease is the first arg (after
`self`). For a cross-arena method, the source arena is paired with its
own lease (`..., source, src_lease, ...`), so caller site reads are
unambiguous about which lease belongs to which arena.

```
def create_token_from_leases(
    self,
    *,
    parents: Sequence[Lease[Any]],
    name: str,
) -> Lease[Any]:
    # 1. Construct UnconditionalLocalLifetime(name).
    # 2. Register end_callback -> self._on_token_end(token).
    # 3. For each parent lease: take it, store a clone on the new token's row
    #    (``parent_leases``), discard the input.
    # 4. Take ull.root_lease, return a clone; discard the taken one so the
    #    lifetime is held up solely by the returned clone (+ any subsequent
    #    children that clone their parent's lease).
    # Grant is ``returned_lease.grant`` and cached on ``LifetimeTokenData.grant``.

def bare_store(self, lease: Lease[B], entity: FxtrCbor) -> FxtrID: ...
def bare_store_flat(self, lease: Lease[B], flat: FxtrEntityFlatData) -> FxtrID: ...
async def bare_load(self, lease: Lease[B], node_id: FxtrID) -> FxtrCbor: ...
async def bare_load_flat(self, lease: Lease[B], node_id: FxtrID) -> FxtrEntityFlatData: ...
def bare_lazily_attach_from_arena(
    self,
    lease: Lease[B_X],
    node_id: FxtrID,
    source_arena: ReadableArena[B_Y],
    key_lease: Lease[B_Z],
    converter: LifetimeConverter[B_X, B_Y, B_Z] | None = None,  # default when B_Y == B_Z
) -> None: ...
def track_from_memory_arena(
    self,
    dest_lease: Lease[B_X],
    src_lease: Lease[B_Src],
    entity_id: FxtrID,
) -> None: ...
```

Each method validates `lease.grant` against the token it resolved to in
`_token_data`, then consumes the lease.

`_on_token_end(token)`:
1. For every node with `token` in `owned_tokens`, remove it. If antichain
    empty, free the node.
2. For every entry in a node's `inheritance_claims` where `X_reg == token`,
    remove it. Then walk `_token_data[token].foreign_leases`: for each `Y`, detach
    the break callback and discard `key_lease`. Drop the whole
    `foreign_leases` map for that token row.
3. Drop all `parent_leases` and `ancestor_leases` on that token's row
    (discarding every clone — cascades up the DAG).
4. Remove the `_token_data[token]` entry, clear any arena state keyed on token.

### `bare_load` load-through-inheritance flow

When the caller's token `X'` does not yet have case-3 on the node, but the
node has some inheritance claim under `X_reg ∈ {X'} ∪ ancestors(X')`:

1. Pick `X_reg` from `node.inheritance_claims` by ancestry check
    (matches current `_pick_inheritance_arena`). Let
    `source_arena = node.inheritance_claims[X_reg]`, `Y = source_arena.token`.
2. Look up `_token_data[X_reg].foreign_leases[Y]`. If `break_reason` is set, raise
    with it as the cause — don't even call the converter.
3. Obtain a fresh `Lease[X_reg]` for `base`:
    - if `X_reg is X'`: clone the caller's lease.
    - else: clone `_token_data[X'].ancestor_leases[X_reg]`.
4. Clone `key_lease`; call `converter(key_clone, base)` → `y_lease`.
5. `flat = await source_arena.load_flat(y_lease, node_id)` — consumes `y_lease`.
6. For each `dep_id` in `flat.deps`: add `X_reg → source_arena` to
    `dep.inheritance_claims` (case-2 claim keyed by `X_reg`) and grow
    `_token_data[X_reg].foreign_leases[Y].claimed_ids`.
7. `bare_store_flat(flat, X')` — adds `X'` to `node.owned_tokens`. This is
    the **case-3 upgrade**: the invariant ("all deps claimed by `X'`") holds
    because the deps' case-2 claims under `X_reg` are covered by `X_reg`
    being an ancestor of `X'`.
8. Return data.

The case-3 upgrade matches current `memory_arena.py` behavior exactly —
we're preserving the invariant, not inventing a new one.

## `ReadableArena[B]` ABC (new signature)

```
class ReadableArena(Generic[B]):
    @property
    def token(self) -> LifetimeToken[B]: ...

    def is_tracked(self, lease: Lease[B], node_id: FxtrID) -> bool: ...
    async def load(self, lease: Lease[B], node_id: FxtrID) -> FxtrCbor: ...
    async def load_flat(self, lease: Lease[B], node_id: FxtrID) -> FxtrEntityFlatData: ...
    def unwrap_provenance(self, lease: Lease[B], node_id: FxtrID) -> FxtrID | None: ...
    def c_node_to_context_data(self, lease: Lease[B], c: FxtrID) -> FxtrID: ...
    def p_node_to_pathroot(self, lease: Lease[B], p: FxtrID) -> FxtrID: ...
    def step_back_p_ctx(self, lease: Lease[B], p: FxtrID) -> FxtrID | None: ...
    def abandon(self, lease: Lease[B], node_id: FxtrID) -> None: ...
    def copy_to_new_arena(self, lease: Lease[B], ids: Sequence[FxtrID]) -> ReadableArena[B]: ...
    def close(self) -> None: ...   # no lease — idempotent best-effort signal
```

```
class CopyTargetArena(ReadableArena[B]):
    async def copy_from_arena(
        self,
        lease: Lease[B],
        source: ReadableArena[B2],
        src_lease: Lease[B2],
        entity_id: FxtrID,
    ) -> None: ...

class ReadWriteArena(CopyTargetArena[B]):
    def store(self, lease: Lease[B], entity: FxtrCbor) -> FxtrID: ...
    def create_c_node(self, lease: Lease[B], context_data: FxtrCbor) -> FxtrID: ...
    def create_p_node(self, lease: Lease[B], pathroot_data: FxtrCbor) -> FxtrID: ...
    def create_u_node(self, lease: Lease[B], wrapped: FxtrID) -> FxtrID: ...
    def track_builtin(self, lease: Lease[B], node_id: FxtrID) -> None: ...
    def lazily_attach_from_arena(
        self,
        lease: Lease[B],
        node_id: FxtrID,
        source: ReadableArena[B_Y],
        key_lease: Lease[B_Z],
        converter: LifetimeConverter[B, B_Y, B_Z] | None = None,
    ) -> None: ...
```

**Grant validation**: every concrete arena checks `lease.grant` against its
own expected grant on each call, raising `LeaseGrantMismatchError` on
mismatch. The ABC does not enforce this — it's a concrete-class obligation.

## `MemoryArena[B]` (concrete)

```
class MemoryArena(ReadWriteArena[B]):
    def __init__(self, *, store: MemoryStore, key: ArenaKey) -> None:
        self._store = store
        self._key = key
        self._closed = False
        # token / expected grant from ``store._arenas[key]`` and ``store._token_data[...]``
```

Methods: validate grant, check `_closed`, call into `_store.bare_*` (which
take the lease and do their own store-level check). `copy_to_new_arena`
creates a new arena bound to the same token (same brand B) — identical to
today's semantics. The liveset is ``store._arenas[key].liveset`` (also exposed
as ``MemoryArena._liveset()``).

Arena state cleanup: `_closed` is per-arena; the liveset row is removed when
the arena is closed. No end-callback registration on the arena object. If the
token dies, subsequent calls raise on lease validation anyway.

## `FxtrScope` (rewritten)

Implemented in `entity_ref_scope.py` (`fxtr_scope.py` re-exports with a
deprecation warning). Lease-backed `_main_lease` / `_main_arena`; keep-alive and
weak child scopes remain for nested-scope coordination.

```
class FxtrScope:
    _token: LifetimeToken
    _main_lease: Lease         # scope owns this clone; matches _main_arena
    _main_arena: MemoryArena   # same token
    _parent_scopes: tuple[FxtrScope, ...]   # for cache lookup only
    _ref_loaded_value_cache: dict[...]      # unchanged

    async def __aenter__(self): ...
    async def __aexit__(self, *exc):
        self._main_arena.close()      # best-effort
        self._main_lease.discard()    # releases our claim; token may die now
        # No wait. If other holders have cloned our main lease, they keep
        # the token alive until they discard too.
```

Constructor behavior:
- Default (non-isolated): parents = `(active_scope,)` + includes.
- `isolated=True`: parents = includes only.
- `_share_token=True`: skip ULL creation, clone the parent scope's lease,
    create a new empty `MemoryArena` bound to the parent's token. On exit,
    discard the cloned lease and drop the arena; do not notify the store
    about token end (parent's lifetime remains). Liveset for this arena is
    separate from the parent's.

Removed API:
- `_token_detached` / any detach path

`freeze_scope` stays unchanged (orthogonal to lease mechanics).

Per-scope methods `lift`, `copy_from_scope`, `track_from_scope`, `abandon`:
continue to exist; internally clone `self._main_lease` as needed and pass it
to arena/store calls. `lift` and `track_from_scope` need only the
destination's lease (ancestry walk inside the store establishes the
claim). `copy_from_scope` additionally clones the source scope's main
lease via `source_scope._main_lease.clone()`.

## Lease-break handling

Policy per sketch: no preemptive subscription to break callbacks on user
leases. The one exception is the store's hold on `key_lease` in
`ForeignLeaseData`:

- On registration: store adds a break-callback that
    (1) stores the break exception into `ForeignLeaseData.break_reason`,
    and (2) emits `logging.error(f"backing store for {Y token name} broke;
    IDs {ids} under memory-store token {X name} are now inaccessible")`.
- Nodes are *not* eagerly removed. Subsequent loads re-surface the stored
    `break_reason` as the cause (preferable to the late `LeaseBrokenError`
    from inside the converter call — the store already knows, so we give
    the caller the real reason upfront).
- Normal cleanup (end of X or claim set empty) detaches the break callback
    before discarding the key lease, so we don't log on intentional
    teardown.

## External-arena callers (migration)

Each of these currently calls `unsafe_lazily_attach_from_arena` or similar.
They must be updated to supply `(source_arena, bridge_lease, converter)`.

Most use the `Z == Y` shortcut: the external resource owns an ULL, produces
a root lease, hands it in as `bridge_lease`, uses the default converter.

| Caller | Pattern |
|---|---|
| `sqlite_store/sqlite_namespace.py` | `SQLiteFxtrSession` owns ULL; arena methods accept leases; namespace hands a clone into `lazily_attach_from_arena`. |
| `git_store/` | Same pattern — session-scoped ULL. |
| `experimental/http/client.py` (`HttpReadableArena`) | Client owns ULL tied to the HTTP client lifetime. |
| `data/remote/transfer_entity_source.py` | `TransferEntitySource` takes an ULL from its caller; converter is default. |
| `data/fxtr_context_api.py` | Uses `_resolve_into_scope` — update to thread lease. |
| `data/store_indexing/index_computer.py` | Two call sites; entity + indexer arenas each need a lease. |
| `data/entity_ref_scope.py` (internal) | Rewritten as part of scope rewrite. |
| `legacy_compat/` | Light touch: UpgradeCache arena gets a token/lease. |
| **At least one test using `AttachedLocalLifetime`** | Covers the bidirectional case where the converter is `AttachedLocalLifetime.exchange_forward` and `Z == key_token`, `Y == output_token`. |

## Deletions

- `rpc_deprecated_v1/` tests: remove. (Code may stay dead but uncompiled-
    against since it still typechecks against the old ABC; simplest to just
    delete the whole `rpc_deprecated_v1/` directory — confirm with user at
    implementation time.)
- Old `MemoryStore.create_token` / `invalidate_token` — replaced by lease-driven paths (`close_arena` still exists for arena teardown).
- `_token_detached`, `_owns_token`, `_share_token` bookkeeping on scope
    (except `_share_token` parameter, which survives as a construction
    variant).
- `unsafe_lazily_attach_from_arena` name — renamed to
    `lazily_attach_from_arena` with safe signature.

## Tests

Full rewrite of:
- `tests/test_memory_arena.py` — use ULLs directly to mint leases; exercise
    the new API surface, node GC via lease discard, DAG parent-child
    lifetime ordering, foreign-lease break logging, `AttachedLocalLifetime`
    converter path.
- `tests/test_entity_ref_scope/test_scope_lifecycle.py` — migrate signatures;
    cover non-blocking scope exit, `_share_token`, weak scopes, scope outliving /
    being outlived by its backing token as appropriate.
- `tests/test_entity_ref_scope/test_frozen_scope.py` — verify freeze still
    works on the lease-backed scope.
- `tests/test_entity_ref_scope/test_caching.py` — unchanged in spirit;
    confirm cache lifecycle decoupled from token.
- `tests/test_entity_ref_scope/test_store_load.py` — mechanical migration.
- Remove `tests/test_rpc_deprecated_v1/`.
- Each affected concrete arena (sqlite, git, http, transfer) gets its lease
    plumbing covered by existing tests updated to new signatures.

Test-level primitive: prefer constructing a real `UnconditionalLocalLifetime`
in each test (via `revoke_after_block()` or `async with`) rather than a
test-only helper. Keeps tests honest about lease semantics.

## Implementation plan (three chunks)

The work splits into three chunks. Each chunk leaves the tree in a testable
state — don't merge chunks together into a single "rewrite everything then
test" push. Within a chunk, many sub-steps are only testable at the end of
the chunk, so don't try to make intermediate sub-steps pass.

### Chunk 0 — Delete `rpc_deprecated_v1` tests

Short, mechanical, up front so we're not carrying dead weight during the
refactor.

- [x] Delete `tests/test_rpc_deprecated_v1/`.
- [x] Confirm the remaining test suite still passes (1763 passed).
- [ ] Note: the `rpc_deprecated_v1/` source directory itself can stay for
    now — it will become dead code relative to the new ABC in Chunk 1.
    Removing it entirely is optional cleanup and can happen opportunistically.

### Chunk 1 — Core rewrite: store, arena ABC, scopes

The big one. End state: `MemoryStore`, `MemoryArena`, `FxtrScope`, and the
arena ABCs fully express lifetimes via leases. **External-arena callers
(sqlite, git, http, transfer, fxtr_context_api, index_computer,
legacy_compat) are NOT migrated in this chunk.** Anything that depends on
them is expected to be broken at the end of Chunk 1; the goal is that the
memory-store-only slice is solid.

Sub-steps (ordered editing sequence, not individually testable).
`memory_arena.py` imports from `abstract_arena.py`, so the ABC rewrite comes
first.

- [x] **`abstract_arena.py` ABC rewrite.** (Completed in branched conversation.)
    - [x] `ReadableArena[B]`, `CopyTargetArena[B]`, `ReadWriteArena[B]` with
        new signatures and `token` property.
    - [x] `FlatEntitySource[B]` narrow ABC factored out (ReadableArena extends
        it; TransferEntitySource implements only this).
    - [x] `LifetimeConverter` protocol; `default_lifetime_converter` helper.
    - [x] Shared `validate_lease_grant` helper at module level (concrete
        arenas call it; no mixin).
    - [x] **`ObservableStore` → `DeepFetchable[B]`**: parameterized by brand,
        `try_lookup(lease, node_id) -> ReadableArena[B] | None`. `backrefs` /
        `EntityBackref` / `OtherBackref` removed outright.
    - [x] **`PersistentPinningBackend[B]` brand-parameterized**: `token`
        property, all methods take `lease: Lease[B]` first;
        `retrieve_pinned` returns `tuple[FxtrID, ReadableArena[B]]` (same
        brand, no new lease).
    - [x] `StreamSink` ABC removed.
    - [x] `LeaseGrantMismatchError` added.
- [x] **`memory_arena.py` full rewrite.**
    - [x] Imports + module docstring.
    - [x] Remove local `LifetimeToken` class; use `fxtr2.leases.LifetimeToken`
        at signature boundaries. DAG bookkeeping (parents, ancestors,
        nodes_in_antichain) moves to store-level dicts.
    - [x] `MemoryStoreNode.inheritance_arenas` → `inheritance_claims`
        (`dict[Token_X_reg, ReadableArena[Y]]`); docstring updated; antichain
        property updated.
    - [x] `ForeignLeaseData` dataclass (`key_lease`, `converter`,
        `claimed_ids`, `deregister_break_callback`, `break_reason`).
    - [x] `MemoryStore.__init__`: `_token_data` (see `LifetimeTokenData`), `_entity_states`,
        bytes tables, `_arena_counter` / `_arenas` / optional `_debug_dropped_arena_tbs`.
    - [x] `create_token_from_leases` (returns `Lease` only; takes each
        parent lease, stores clones in `_parent_leases` and the transitive
        closure in `_ancestor_leases`; discards the store's own root lease).
    - [x] `_on_token_end` registered as end-callback; free nodes, discard
        parent/ancestor/foreign leases, detach break callbacks.
        *Known bug*: per-node `arena.abandon(...)` call currently passes a
        token where a lease is expected — switch to `arena.close()`-only
        and remove the per-node abandon step.
    - [x] `_take_and_validate(lease) -> (token, taken)` helper; concrete
        methods validate grant via `validate_lease_grant` and consume the
        caller's lease.
    - [x] `_make_base_lease(caller_token, caller_lease, x_reg)` — sources
        `Lease[X_reg]` per the no-cycle rule (clone caller's if `x_reg is
        caller_token`, else clone from `_token_data[caller_token].ancestor_leases`).
    - [x] `_is_strict_ancestor` / `_is_ancestor_or_equal` helpers.
    - [x] Antichain helpers updated: `_is_dominated_by_antichain`,
        `_remove_dominated_from_antichain`, `_add_owned_token`,
        `_is_node_claimed_by`, `_has_case3_access` — all using the new
        store-level DAG state; `_release_foreign_claim` discards
        `key_lease` once the claimed-ID set empties.
    - [x] `_get_or_create_node`, `_populate_node_data`, `_free_node` —
        field rename + docstring refresh.
    - [x] `bare_lazily_attach_from_arena` (lease-first) +
        `_register_foreign_lease` (idempotent `(X_reg, Y)` registration with
        break callback) + `_invoke_converter` (produces fresh
        `Lease[Y]`; re-surfaces stored `break_reason` if key lease broke).
    - [x] `_adopt_inheritance_arena_claim` (+ `_or_none`) updated: uses
        `inheritance_claims`, threads `caller_token`/`caller_lease` for
        unwrap-chain discovery, grows `ForeignLeaseData.claimed_ids`.
    - [x] `_pick_inheritance_arena` → `_pick_inheritance_claim` (returns
        `(X_reg, source_arena)`).
    - [x] `_ensure_own_derived` signature updated with `caller_lease`;
        internal arena-query calls thread Y-leases via a `_y_lease()`
        closure; `_adopt_inheritance_arena_claim` call sites pass
        `caller_token` + `caller_lease`.
    - [x] `_ensure_own` signature updated with `caller_lease`; loads via
        converter-produced Y-lease; dep adopt and `_bare_store_flat_impl`
        calls wired up.
    - [x] Public `bare_store` / `bare_store_flat` / `bare_load` /
        `bare_load_flat` wrappers take `Lease[B]` first, delegate to
        `_bare_*_impl` helpers that take tokens.
    - [x] `track_builtin`, `create_c_node`, `c_node_to_context_data`,
        `create_p_node`, `p_node_to_pathroot`, `step_back_p_ctx`,
        `create_u_node`: lease-first signatures; `_create_u_node_impl`
        internal helper for the recursive call site in `_bare_load_impl`.
    - [x] `unwrap_provenance` on `MemoryStore` + `MemoryArena` (lease-first).
    - [x] `try_lookup_for_token` → `try_lookup` matching `DeepFetchable[B]`
        (validates lease, returns a MemoryArena sharing the caller's brand).
    - [x] Deleted `backrefs` method and all `EntityBackref` /
        `OtherBackref` references.
    - [x] Arena registry on the store (`ArenaKey`, `MemoryArenaState`, `new_arena`,
        `track_in_arena`, `untrack_from_arena`, `close_arena`, `weakref.finalize` /
        `_finalize_arena_object`, optional `DEBUG_DROPPED_ARENAS`). Liveset on
        `MemoryArenaState`; `MemoryArena` has `_closed` only.
    - [x] `track_from_memory_arena`: lease-first signature with
        `(dest_lease, src_lease, entity_id)` per convention; `_impl` method
        holds the DAG walk.
    - [x] `_copy_from_external_arena_impl`: lease-first, fully wired. The
        worklist loop holds the caller-provided `src_taken` alive for its
        duration and clones it before each `source.copy_to_new_arena` /
        `tmp_source_arena.load_flat` call. Signature takes already-taken
        leases from `MemoryArena.copy_from_arena` (avoids a double-take).
    - [x] `MemoryArena[B]` class full rewrite: `__init__(store, key)`; token/grant
        from store; `_liveset()` reads store row; `_closed`; lease-per-call methods;
        `_track_from_store` / store validation.
    - [x] Deleted `MemoryStreamSink`, `EmittedItems`, `EmittedSubstream`,
        `_make_copy_from_arena`.
    - [x] Fixed the `_on_token_end` per-node abandon bug by dropping the
        per-node calls and relying on `arena.close()` for bulk teardown.
    - [x] Module imports cleanly (`uv run python -c "import
        fxtr2.data.memory_arena"`).
- [x] **`entity_ref_scope.py` scope rewrite.**
    - `FxtrScope` with `_main_lease` / `_main_arena`; keep-alive and weak-scope
        machinery retained (`new_weak_child_scope`, etc.); detach bookkeeping
        removed.
    - `_share_token=True` variant: clone parent's lease, fork an empty
        `MemoryArena` on the same token.
    - `_main_lease` + `_main_arena` naming.
    - `lift` / `copy_from_scope` / `track_from_scope` / `abandon` on the
        new API.
    - Internal callers of old `unsafe_lazily_attach_from_arena` migrated.
    - `freeze_scope` preserved.
- [x] **Delete dead machinery now unused** by the rewritten core:
    - `_token_detached`, old `create_token`/`invalidate_token` paths (and related
        scope detach helpers), per earlier bullets.
    - `StreamSink` ABC and `MemoryStreamSink` implementation — never
        used outside the class itself; experimental infrastructure that
        was never adopted. Drop the class, its imports, and the
        `from fxtr2.data.abstract_arena import StreamSink` usage inside
        `memory_arena.py`.
- [x] **Rewrite tests for the core slice.**
    - `tests/test_memory_arena.py` — exercise the new API with real ULLs,
        including the `AttachedLocalLifetime`-based converter test for a
        bidirectional attachment. Try to preserve the semantic coverage of
        each existing test; where a test exercises a concept that no
        longer maps cleanly (e.g. explicit `invalidate_token` sequencing),
        drop it and list it in this plan under a new
        "Tests dropped during migration" subsection with a one-line note.
    - `tests/test_entity_ref_scope/` — migrate signatures; keep weak-scope
        coverage where still relevant; add non-blocking-exit, `_share_token`,
        scope-outlives-token / token-outlives-scope cases as needed. Preserve
        `test_frozen_scope.py`, `test_caching.py` coverage. Migrate
        `test_experimental_deep_fetch.py` to the
        `DeepFetchable[B]` interface (drop the `backrefs` mock; change
        `MockObservableStore` → `MockDeepFetchable[B]`). Same rule:
        non-portable tests noted in this plan.
- [x] **Update `CODE_OVERVIEW.md` and `fxtr2/leases/LOCAL_CODE_OVERVIEW.md`**
    to reflect the new core. Downstream sections referring to the
    concrete arenas may still describe the old signatures temporarily —
    call that out in each affected `LOCAL_CODE_OVERVIEW.md`.
- [x] **Run `tests/test_memory_arena.py` and `tests/test_entity_ref_scope/`**.
    Chunk-1 success criterion: these pass; the rest of the suite is
    allowed to be broken.

### Chunk 2 — Migrate external-arena callers

Each migration is one-or-two modules at a time; run the module's own tests
after each unit.

Recommended order:

- [x] **`fxtr_context_api.py` + `sqlite_store/`** together (they're
    entangled; `SQLiteFxtrNamespace` is the main `FxtrNamespace` subclass
    that exercises the context API). Session ULL pattern: session owns an
    `UnconditionalLocalLifetime`, namespace arena exposes its token, the
    pin/retrieve paths thread key leases through `lazily_attach_from_arena`
    with the default converter. Run `tests/test_sqlite_store/` and
    `tests/test_fxtr_context_api.py`.
- [x] **`store_indexing/index_computer.py`**: both attach sites migrated.
    Run `tests/test_store_indexing/`.
- [x] **`git_store/`**: same pattern as sqlite. Run `tests/test_git_store/`.
- [x] **`legacy_compat/`**: UpgradeCache arena gets a token + lease. Likely
    shallow. Run `tests/test_legacy_compat/`.
- [x] **`experimental/http/client.py`**: `HttpReadableArena` gets a token
    and the HTTP client lifetime becomes an ULL. Run HTTP tests.
- [x] **`data/remote/transfer_entity_source.py` + `projects/behaviors_hub_*`** —
    done; see [`transfer_and_hub_fix_plan.md`](transfer_and_hub_fix_plan.md).
    Introduced `STATIC_LIFETIME` for the transient-data case, rewrote
    `TransferEntitySource` with a two-set (data / tracked) design under that
    lifetime, widened `PersistentPinningBackend.pin_entity_from_arena` to
    accept `FlatEntitySource`, and migrated the hub client
    (`store_sync.py`) plus server (`EntityService`, ingest path, workspace
    routes) to the lease-first APIs.
- [x] **Remaining stragglers**: grep for any surviving
    `unsafe_lazily_attach_from_arena` references. All should be gone.
    Also grep for `ObservableStore` / `EntityBackref` / `OtherBackref` /
    `.backrefs(` — all should be gone (renamed to `DeepFetchable[B]`,
    backrefs dropped). Delete any concrete `backrefs` method
    implementations on each namespace/store class touched above.
- [x] **Update per-module `LOCAL_CODE_OVERVIEW.md`** as each unit is
    touched (`sqlite_store`, `git_store`, `remote`, `experimental`,
    `store_indexing`).
- [x] **Full test run** (`uv run pytest -q` — 1613 passed). `pytest -n auto` can
    intermittently fail `TestGitNamespaceBackendExportImport::test_export_import_roundtrip_notar`
    (passes under `-n 0`); treat as flaky / ignore unless someone wants to chase xdist isolation.
- [x] **Final `CODE_OVERVIEW.md` reconciliation** — arena ABC summary,
    scope summary, note that memory store is lease-backed, updated module pointers.

## Open questions / things to revisit

- **Ancestor-lease precomputation cost.** Each token row’s `ancestor_leases` map
    eagerly clones a lease for every strict ancestor at token-creation time,
    which is O(ancestors) work and O(ancestors) leases held per token. For deep
    scope chains this could matter. Alternatives: walk `parent_leases`
    transitively on demand (O(depth) per lookup, no extra bookkeeping), or cache
    lazily on first access. Precomputation is simplest; revisit if benchmarks
    flag it.
