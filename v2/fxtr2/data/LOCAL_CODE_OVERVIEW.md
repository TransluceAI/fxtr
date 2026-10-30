# Data Local Code Overview

Detailed documentation for the core data-layer modules. For a high-level
overview, see the root `CODE_OVERVIEW.md`. The five modules covered in
detail here (`abstract_arena.py`, `arena_util.py`, `entity_ref_scope.py`,
`memory_arena.py`, `fxtr_context_api.py`) don't have their own subpackage
LOCAL_CODE_OVERVIEW because they sit at the top of the data tree.

For details on the subpackages — `tsv_namespace/`, `layered_file_store/`,
`remote/`, `sqlite_store/`, `store_indexing/` — see the
LOCAL_CODE_OVERVIEW.md inside each. The other top-level files
(`entity_core.py`, `system_entities.py`, `file_util/`) are short enough
that the root CODE_OVERVIEW description is sufficient.

## abstract_arena.py

Defines the abstract interfaces. Arenas are the lease-gated tier: every
arena is parameterized on a lifetime brand `B`, exposes a `token:
LifetimeToken[B]` property, and takes a `Lease[B]` as the first argument
of every method (consumed on each call). Namespace backends
(`PersistentPinningBackend` / `IndexablePinningBackend` / `DeepFetchable`)
are also brand-parameterized and expose `token`, but their own methods do
**not** take a backend lease — backends manage their own drain via their
context-manager state. Callers obtain a `Lease[B]` for arenas returned by
a backend via a concrete `backend.get_lease()` helper.

- **`FlatEntitySource[B]`**: Minimal base for reading flat entity data
  (`token`, `is_tracked`, `load_flat`, `copy_to_new_arena`, `close`).
  Used by `TransferEntitySource` directly; `ReadableArena` extends it.
- **`ReadableArena[B]`**: Can load entities, check tracking, unwrap
  provenance nodes.
  - `load_flat()`: Load entities without deserializing, returning raw
    bytes and dependencies.
- **`CopyTargetArena[B]`**: Can copy entities from another arena
  (`copy_from_arena(lease, source, src_lease, entity_id)` — source lease
  paired with the source arena).
- **`ReadWriteArena[B]`**: Can also store new entities synchronously;
  gains `lazily_attach_from_arena(lease, node_ids, source, bridge:
  LocalBridge)` (single ID or `Collection[FxtrID]`) for safely mounting a
  source arena. The `bridge` is a `LocalBridge` from
  `fxtr2.leases.lease_bridges`; same-store sources are auto-detected by
  the `MemoryStore` implementation and stored as sentinels.
- **`PersistentPinningBackend[B]`**: Persistent storage with named pins
  and listing; `retrieve_pinned(key)` returns `(FxtrID, ReadableArena[B])`
  sharing the backend's brand. Pin/unpin/list methods take no backend
  lease; `pin_entity_from_arena(key, entity_id, source, src_lease, *,
  overwrite)` still takes a source-arena lease.
- **`DeepFetchable[B]`**: Protocol for *external* deep-fetch sources
  registered via `FxtrScope.register_deep_fetch_source`. Single method:
  `try_lookup(node_id) -> ReadableArena[B] | None`. The global
  `MemoryStore` is *not* a `DeepFetchable` — it serves many tokens at
  once and exposes the polymorphic `try_lookup_with_lease(lease,
  node_id)` instead.
- **`LifetimeConverter[B_X, B_Y, B_Key]`**: Protocol for `(key:
  Lease[B_Key], base: Lease[B_X]) -> Lease[B_Y]` — used by
  `lazily_attach_from_arena` to bridge foreign-arena lifetimes.
- **`default_lifetime_converter`**: Default converter used when `B_Y ==
  B_Key` (discards `base`, returns `key` unchanged).
- **`validate_lease_grant(expected, lease)`** + **`LeaseGrantMismatchError`**:
  Shared grant-validation helper used by concrete arenas.
- **`DepKind`**: Enum for dependency relationship types (STRONG_REF,
  WEAK_REF, CTX_NODE, BACKSTEP, UNWRAPS_TO).
- **`FxtrEntityFlatData`**: Flat representation of entity data and
  dependencies (raw bytes + deps list).

## arena_util.py

Utility functions for traversing entity data:

- `visit_ids()`: Call a function for each ID in an entity.
- `visit_and_project_ids()`: Transform all IDs in an entity.
- `visit_and_project_ids_in_canonical_cbor_order()`: Same but in CBOR
  serialization order (important for p-nodes).
- `async_visit_and_project_ids_in_canonical_cbor_order()`: Async version.

The visitor helpers treat `RawFxtrEntityMissingMarkerSentinel` as a
no-op leaf so storage-path traversal works on dicts that haven't been
replace-stamped yet.

## entity_ref_scope.py

The unified core module for entities, entity references, and scopes.

### Entity / EntityRef

- **`Entity`**: Abstract base class for all serializable entities.
  - `_fxtr_store_impl() -> EntityRef[Self]`: Abstract method for
    serialization. Subclasses serialize to CBOR, store via
    `scope.store_cbor()`, and return an `EntityRef` declaring the default
    load type.
  - `store(scope) -> EntityRef[Self]`: Convenience method that delegates
    to `_fxtr_store_impl` via the scope.
- **`EntityLoadHandler[T]`**: Protocol for entity deserialization
  handlers.
  - `supports_entity_type(type_id, target_type) -> bool`: Check if the
    handler can deserialize a given type ID.
  - `from_lowlevel_entity_data(cbor, target_type) -> DeserializedEntity[T]`:
    Deserialize CBOR to a Python object.
- **`register_entity_load_handler(for_type, handler)`**: Register a
  handler in the global entity load registry.
- **`EntityRef[T]`**: A reference to an entity that can be loaded
  lazily.
  - Holds `entity_id: FxtrID` and `target_type: EntityTypeAnnotation[T]`
    (can be a union, generic alias, etc.).
  - `load(scope)`: Load the entity via the scope.
  - `val`: Synchronous access to a cached value (raises
    `EntityNotLoadedError` if not cached).
  - `copy_to(scope)`, `copy_from(scope)`, `lift(scope)`: Scope operations.
  - `replace_target_type(new_type)`: Create a new ref with a different
    target type.
- Equality / hashing on `EntityRef` is provenance-blind (matches
  `FxtrID`); the provenance dimension lives only in the scope-side
  cache key tuple.

### Service primitives

- **`ServiceRef[T]`** — frozen dataclass `(handle: IRHandle, target_type:
  ServiceTypeAnnotation[T], service_type_id: FxtrID)` resolved via `await
  ref.resolve(scope)` or `await scope.resolve_service(ref)` (the home
  scope **must** have a task group — CM-entered, not lifespanless). The `service_type_id` is the
  wire-portable runtime type identity (an arbitrary `FxtrID` minted by
  the impl — typically `FxtrID.builtin("untyped_service")` for the
  dynamic case, but could be a stored type-descriptor entity for typed
  services). It is *not* required to be tracked in any scope that
  knows the ref — the wire ships only the data-id half (FxtrID
  equality ignores provenance), so callers treat it as an opaque
  hash. To introspect the loadable form (suitable for
  `EntityRef.load`), dispatch `$fxtr.type` over the handle.
- **`ServiceKey`** — frozen dataclass `(handle, type_id)` used as the
  unit of identity for the scope's `_known_handles` /
  `_CapBridgeEntry.handles` sets.
- **`FxtrServiceImpl[T]`** — bare ABC: subclasses implement
  `_mount_as_fxtr_service(scope, *, task_group) -> ServiceRef[T]`.
  Concrete dispatch + method-discovery (`@service_method`,
  `UntypedServiceImpl`, etc.) lives in `fxtr2/services/`.
- **`ServiceProxyContext`** — the scope-side API surface a resolved
  proxy uses against its home scope: `mint_lease_for_key(key)`,
  `fork_arena_handle_for_ids(ids)`, plus public `home_scope`,
  `proxy_handle`, `service_type_id` (and a `proxy_key` property
  combining handle + type_id) attributes. Built once per resolve and
  handed to the registered `ServiceResolver`, so proxies don't reach
  into `FxtrScope` private members. Resolvers are looked up by
  `target_type` in `_SERVICE_RESOLVER_REGISTRY`; register concrete
  resolvers via `register_service_resolver(for_type, resolver)`.
### FxtrScope

A scope governs the lifetime of retrievable IDs. **Construction is inert** —
`FxtrScope(...)` records configuration only (no token, arena, parent leases,
or marker). It becomes usable on *entry*: `async with FxtrScope() as scope:`
(opens a task group + closing event; can host services) or
`FxtrScope(isolated=True).lowlevel_start_lifespanless()` (no task group; the
root scope + lowlevel callers; cannot host services). There is no autostart
and no `mountable=` axis.

Construction parameters (config only; applied at entry):
- `include`: parent scopes to inherit from (each becomes a derived-token input
  at entry).
- `isolated=True`: doesn't inherit from / activate the active scope.
- `provenance_context: FxtrID | None = None` (kw-only): sets the scope's
  `$fxtr.ctx` marker. Required to be a `c`-node that unwraps to `"."`.
  The scope's marker becomes `FxtrID(".",
  _provenance_parent=provenance_context)`, which propagates to every
  entity stored into the scope (and is inherited by child scopes that
  don't supply their own). When `None`, the scope copies its first
  parent's marker (or, for root, falls back to bare `"."`).
- `provenance_context_from_scope: FxtrScope | None = None` (kw-only):
  names the scope where the c-node is already tracked; the c-node is
  sync-copied via `track_from_memory_arena` into the new scope's main
  arena up front (so the store/load hot path doesn't have to walk
  parents for it). When `None`, the constructor walks parents to find a
  covering source. Typical pattern for cross-tree c-node sourcing: build
  the c-node in a sibling scope with the same parents as the target,
  then construct the target with both kwargs set.
- `store_provenance: Literal["auto", "if_marked", "strip"] = "auto"`
  (kw-only): controls `store_cbor`'s child-provenance handling. `"auto"`
  (default) preserves provenance whenever it's reachable — if the
  cbor's marker is bare but any non-marker child carries a
  `_provenance_parent`, the marker is substituted with this scope's
  layered marker (its own c-node, or a lazily-minted fallback c-node
  built from `FallbackContextMarker(scope_name=self._name)`). The
  fallback c-node is cached on `self._fallback_marker` after first use
  and dedup'd by `scope_name` across scopes. `"if_marked"` is the old
  default: prov path iff the cbor's marker carries a provenance
  parent. `"strip"` always discards child provenance on store and
  refuses to inherit a parent's provenance marker (incompatible with
  `provenance_context=`). Mode is per-scope; does not inherit from
  parent. Loads always preserve provenance regardless of mode.
- `name: str | None = None` (positional, third arg): debug name. When
  `None`, inferred from the caller's frame (`inspect.currentframe()
  .f_back.f_code.co_name`). Explicit empty string `""` is kept
  verbatim — only `None` triggers inference. Used in
  `repr(scope)`, lifetime-token names, and as the `scope_name`
  payload of any fallback c-node minted by `"auto"` mode.

Entry primitive: `async with scope.open_with_explicit_leases(parent_leases=…,
extra_derived_leases=…)`. The normal `async with scope:` *is* this, called with
`parent_leases` cloned from each parent's in-context authorization;
`__asynccontextmanager__` delegates to it. The explicit form lets callers mint
the token from leases they supply rather than from in-context authorization —
cross-task, opaquely-passed, or **draining-but-open** parents (the dispatch
case). `extra_derived_leases` supplies additional derived-token inputs keyed by
**token** (not parent scope) — for tokens the scope must gate on but that have
no `FxtrScope` object (e.g. dispatch arg-ServiceRef tokens); each gets a case-3
dependent ancestor entry but is not a parent scope. An `extra_derived_leases`
token that duplicates a parent token (or the freshly-minted key token) is
**tolerated** — the redundant lease is discarded and the token keeps its
parent/key treatment (not recorded as an extra), so callers can pass the full
candidate set without pre-deduping. (`_with_derived_lifetime` is gone — every
scope is uniformly derived now.)

Key methods: `store()`, `store_cbor()`, `load()`, `load_cbor()`,
`lift()`, `copy_from_scope()`, `track_from_scope()`, `abandon()`,
`deep_load()`, `clear_cache()`, `is_tracked()`,
`register_deep_fetch_source()`, plus `aclose()` (drain) and the sync
`force_close()` (revoke immediately). Layer-crossing helpers
`extract_provenance_parent(data_with_prov, *, extract_as_u_if_missing=False)`
(drop the data view, return the bare provenance node) and
`embed_as_provenance_parent(prov_node)` (look up `unwraps_to`, return
`FxtrID(unwrapped, _provenance_parent=prov_node)`) are 1-1 inverses for
moving between the data and provenance layers; both validate the input
IDs are tracked in the scope. `extract_provenance_parent` is the
canonical "project an id to its bare provenance form" primitive
(shared by `store_cbor`'s prov path, the `preserve_provenance`
persistence cap, and the provenanced layered file store writer): with
`extract_as_u_if_missing=True` a *bare* input (no `_provenance_parent`)
is wrapped to `u(id)` via `create_u_node` instead of raising, so a
caller that always wants a prov-node id back uses a single call. The
branch is purely on the layering, never on the id's tag — a bare
`n`/`c`/`p`/`u` node is wrapped just like a bare `d`. Provenance-node introspection helpers
`c_node_to_context_data(c_node)` (return the d-node a c-node was built
from) and `step_back_p_ctx(p_node)` (return the p-node parent in the
path, or `None` for a pathroot p-node) are thin scope wrappers around
the corresponding sync `MemoryArena` methods — they apply the scope's
own lease and lifecycle / tracked checks before delegating.
`create_u_node(wrapped_id)` (return a bare `u`-tagged provenance node
that wraps `wrapped_id`) follows the same scope-wrapper pattern: it
validates `wrapped_id` is tracked here and carries no
`_provenance_parent`, then delegates to `MemoryArena.create_u_node`.
Pair with `embed_as_provenance_parent` to materialize the canonical
"data with trivial provenance" layered form `(wrapped_id,
u(wrapped_id))`. `create_c_node(context_data)` is the corresponding
helper for c-nodes: a thin wrapper over `MemoryArena.create_c_node`
that mints a c-node from a d-node cbor (with the required `value`
field). The returned bare c-tagged FxtrID is immediately usable as
another scope's `provenance_context`. `create_p_node(pathroot_data)`
is the matching helper for path-provenance (p-) nodes (thin wrapper
over `MemoryArena.create_p_node`); both auto-lift referenced ids.

#### Provenance-aware op behavior

A FxtrID with `_provenance_parent` is treated as a single conceptual
unit. `store_cbor` dispatches on `self._store_provenance`:

- `"strip"` → bare path always: every child id is projected to its
  bare data form before the arena store.
- `"if_marked"` → prov path iff the cbor's `$fxtr.ctx` marker carries
  a provenance parent; otherwise bare path.
- `"auto"` (default) → prov path iff the marker has a prov parent or
  any non-marker child reference does. When the trigger is a
  prov-bearing child (not the marker), the marker is substituted with
  `self._get_or_mint_fallback_or_own_marker()` — the scope's own
  layered marker if it has a `provenance_context`, otherwise a
  lazily-minted fallback c-node built via `self.create_c_node(...)`
  from a `FallbackContextMarker(scope_name=self._name)` payload. The
  fallback c-node lives in the scope's main arena under its main
  lease and is cached on `self._fallback_marker`.

The **bare path** projects every child id to its bare data form
before the arena store. The **prov path** projects every ID to its
provenance form (existing prov parents kept; bare IDs u-wrapped via
`MemoryArena.create_u_node`); the arena stores the resulting n-node
and the returned FxtrID is `(d_id, _provenance_parent=n_id)`. `load`
/ `load_cbor` rewrap children with their layered form using sync
`unwrap_provenance`. `is_tracked(layered)` checks both halves;
`abandon(layered)` releases both. `lift` / `copy_from_scope` /
`track_from_scope` flow only the provenance parent — `unwraps_to`
propagation pulls data along — followed by a sync
`unwrap_provenance` to add the data ID to the destination's liveset
(so `is_tracked` reads symmetric).

#### Cache key shape

`_ref_loaded_value_cache` is keyed on `(data_id, provenance_parent)`
(with the inner dict still keyed on the load target type), and
`_refs_loaded_since_abandon` uses `(data_id, prov_id, target_type)`
tuples. Helpers `_cache_key_for_id` and `_ref_load_key` build these
keys.

#### Lease-backed lifetimes + authorization

At entry each scope mints a `_derived: DerivedLocalLifetime` over a private
`_key_ull: UnconditionalLocalLifetime` plus each direct parent's main token
(uniform — no ULL-vs-derived split), registers the output token via
`register_token`, mints the first authorization lease via `exchange_forward`,
and **retains it as the scope-level base `_scope_auth_lease`** while discarding
the bare key root (the `_key_ull` *object* is kept as a `force_close` revoke
handle). The scope has a `_main_arena` bound to that token.

Authorization is two-layered (see `new_scope_design.md` §2):
- **Base**: `_scope_auth_lease`, a plain attribute — usable from any task while
  the scope is open. Dropped to `None` at `aclose`.
- **Overrides**: `with scope.authorize(lease) as auth:` binds a per-context
  override in the single module-level `_AUTH_OVERRIDES` contextvar (copy-on-write
  `{id(scope): Lease[main]}` map), touched *only* by `authorize` — never by
  ordinary entry. It yields an `AuthorizationHandle` (`auth.discard()` drops
  *this* override's lease early; idempotent).

`_current_auth_lease()` resolves **base-first**: a live base authorizes any
context (overrides *union* with it, they don't override it); the override is
consulted only when the base is gone (draining), where a live override wins and
a present-but-discarded one is a hard error; else not-authorized. So a stale
override inherited by a spawned subtask via context-copy can't revoke access a
live base grants. Every `_main_arena.X(...)` op threads
`self._current_auth_lease().clone()`. Discard is split: `auth.discard()` (a
per-context override) vs `discard_base_authorization()` (the scope-level base,
the owner relinquishing → drain). Public plumbing: `clone_authorization()`,
`decompose_authorization(lease) -> {token: Lease}` (a flat map of *all* vias —
the key via at `key_token`, each parent via at `parent.scope_token`, each extra
via at its token), and `compose_authorization(leases: {token: Lease})` (the
inverse; the key set must match the scope's full derived-input set exactly). The
public `scope_token` (main token) / `key_token` (private key token) accessors are
how callers index a decomposed map.

Child-keeps-parent-alive is structural: a live `Lease[child.main]` carries each
`Lease[parent.main]` as a via. The per-parent (and per-extra-token) ancestor
entries are case-3 `DependentLocalBridge`s (`subtake` from the child's own
authorization, nothing stored at rest). The memory store does not track parent
relationships at all.

#### Lifecycle state

Persistent facts: `_started` (set at entry, after the token is created),
`_closed` (set when teardown completes; for idempotency), and the token's own
`ended()`. `is_open()` = token created & not ended; `is_authorized()` = this
context resolves a live override-or-base; `check_authorized()` raises with the
most specific error (not-started / closed / discarded-override /
not-authorized). A scope is *draining* while its token is alive but the base
lease has been dropped to `None`: the owning context (and any other context
without an override) is rejected by `check_authorized`, while in-flight contexts
holding
overrides keep working.

#### Drain semantics

`aclose` drops the base `_scope_auth_lease` (→ `None`, a global attribute write
that rejects new ambient use everywhere) then awaits the derived token's drain
— the token ends once the last `Lease[main]` clone (and thus the last key via)
is released. `force_close` revokes the derived token **and** the `_key_ull`
(breaking any externally-split key leases) synchronously. Closing a never-opened
(inert) scope raises. The async CM wraps these in `try: yield; except:
force_close; raise; else: aclose`.

#### Within-lifespan machinery (lifespans)

Every CM-entered scope opens a task group + a `_closing_event` at entry.
`attach_lifespan_to_scope(cm)` spawns `_run_lifespan` via `tg.start` (so `cm` is
fully entered before it returns): the runner enters `cm` under
`authorize(setup_lease)` + `as_active_scope()` (setup is authorized + active),
signals readiness with `cm`'s yielded value, **discards its authorization right
after** so the parked task holds no `Lease[main]` (a standing one would wedge
the token-drain wait), parks on `_closing_event`, then on wake exits `cm`
(teardown — post-drain, unauthorized). `aclose` sets `_closing_event` after the
drain, closes the arena, then waits on `_lifespans_closed_event` (a latch the
last finishing lifespan sets, tracked by `_lifespan_count`) so it returns only
once lifespans have torn down — it can't await the task group itself (it runs
inside it). Multiple lifespans tear down concurrently (not LIFO). `force_close`
is synchronous (sets the event + closes the arena; the CM stack's task-group
unwind cancels parked lifespans). Lifespanless scopes have no task group / event
and reject `attach_lifespan_to_scope`.

#### Deep-fetch sources

`register_deep_fetch_source(source, bridge: LocalBridge)` stores a
`DeepFetchSourceEntry` (source + bridge). `_deep_fetch_scan_sources`
calls `source.try_lookup(node_id)` (no per-call backend lease); on a
hit, the result is mounted into this scope's arena via
`lazily_attach_from_arena`, threading a clone of the stored bridge so
subsequent loads re-run the same minting flow. No deregistration —
namespaces must outlive the scope they registered on; the stored
bridge's at-rest leases are discarded at scope exit.

#### Live-capability bridges

Every scope (with a task group or not) hosts capability and ancestor-lease
mounts via two parallel structures: `_known_handles: set[ServiceKey]`
(which `(handle, type_id)` pairs this scope is willing to resolve) and
`_cap_bridges: dict[LifetimeToken, _CapBridgeEntry]` (each entry holds a
`LocalOrPipelinedBridge` plus the set of `ServiceKey`s registered
against it, plus an `is_ancestor_or_arena_owner: bool` flag).
`ServiceKey` is the unit of identity throughout — the bridge is
per-token (lease-minting machinery), but the keys authorize specific
`(handle, type_id)` pairs against it. The flag distinguishes **service
entries** (created by `mount_*` / `attach_*` / eager-lift; torn down
when the last key is forgotten) from **ancestor / arena-owner entries**
(created at construction for each direct parent + transitive ancestor,
and by the `_register_external_arena_bridge` helper for namespaces that
own arenas whose lifetime is independent of any ancestor scope;
preserved through `forget_service` because their lease is structural,
not service-traffic state). The four bridge variants live in
`fxtr2/leases/lease_bridges.py` on the `Local` vs `Pipelined` (output
shape) × `Independent` vs `Dependent` (single stored lease vs `inputs +
converter`) axes.

The lift path consumes the ancestor entry's `IndependentLocalBridge`:
`FxtrScope.lift(node)` walks parents to find a scope whose arena tracks
the node, clones the matching ancestor bridge, and calls
`_main_arena.lazily_attach_from_arena(self_lease, [node], parent_arena,
cloned_bridge)` — the memory store auto-detects same-store and stores a
`_SameStoreSentinel` keyed on this scope's token.

**Lease-passing rule**: every method that operates on a scope's main
token (`_is_tracked_internal`, the per-ancestor query in
`_lift_unchecked` and `_deep_fetch_*`) takes the relevant `Lease` as an
argument; child scopes mint a `Lease[ancestor.main_token]` from their
*own* bridge graph via `_mint_ancestor_lease(token)` rather than relying
on the ancestor's base authorization (which is dropped as soon as `aclose`
starts and is therefore not usable from a child scope's perspective
during drain). `_mint_ancestor_lease` for the scope's own token clones
`_current_auth_lease()`.

Mount methods (require a task group — i.e. CM-entered, not lifespanless):
`mount_lowlevel_cap_as_service(handle, T, service_type_id)` (installs
an empty-input identity `DependentLocalBridge`; requires `handle.token
is self._token`) and `attach_bridged_lowlevel_cap(handle, T,
service_type_id, bridge)` (takes any of the four bridge variants).
Neither requires `service_type_id` to be tracked in this scope —
the type_id is wire-portable identity (consumers wanting the loadable
form dispatch `$fxtr.type`); impls typically track it anyway so the
`$fxtr.type` framework method can return it through the per-call
return arena. Both return a `ServiceRef[T]` carrying handle +
target_type + service_type_id. The bridge is idempotent across mount
calls on the same token. `mount(impl)` is a thin wrapper that
delegates to `impl._mount_as_fxtr_service(self)`. `forget_service(ref)`
removes a known `ServiceKey` and drops the bridge if no other key uses
it (preserving `is_ancestor_or_arena_owner=True` entries regardless).

Resolution (`scope.resolve_service(ref)` / `await ref.resolve(scope)`)
walks `self → parents` for the closest scope with the matching
`ServiceKey` in `_known_handles`; `_find_bridge_for_service_key` /
`_lift_bridge_for_service_key` are the public-internal entry points.
Bridges are torn down in the same token-end callback that releases
`_deep_fetch_sources` — they stay live throughout drain so in-flight
dispatches still resolve.

#### Cross-tree bridging (`bridge_service_from_scope`)

`await dest.bridge_service_from_scope(ref, source_scope, keepalive_via=…)`
registers `ref` on `dest`. The type_id is *not* copied into `dest` — it's
wire-portable identity, not arena-tracked content; consumers wanting the
loadable form dispatch `$fxtr.type`. Strict on duplicate (raises if the
`ServiceKey` is already in `dest._known_handles`). Returns `None`.
`await ref.bridge_from_scope(source_scope, into_scope=None, keepalive_via=…)`
is a convenience that defaults `into_scope` to the active scope.

**`keepalive_via` is required** — it declares how `source` is kept alive
relative to `dest`. The contract is about **block nesting** (async-task
`async with` nesting), not the scope-parent relationship: `source` must never be
forced to finish closing before `dest`.

- `"independent_lease"` — `dest` holds a standing `Lease[ref.handle.token]`
  (wrapped `IndependentLocalBridge` / `IndependentPipelinedBridge`) until `dest`
  drains, so `source` closes *after* `dest`. `source` must **not** be block-nested
  *inside* `dest` (nesting `dest` inside `source`, or running concurrently, is
  fine). Today's behavior, now explicit.
- `"concurrent_child_bridge"` — `dest` and `source` **close together**. Requires
  (and checks) `dest` is a direct scope-parent of `source`, `ref` is mounted on
  `source` itself, and `source` has no extra non-parent derived inputs. `dest`
  installs a `DependentLocalBridge` for `source.main` that holds source's *key* +
  other-parent vias (not a standing `Lease[source.main]`), so `source` can't
  self-drain — closing `dest` cascades `source` closed. The two **must drain in
  concurrent tasks** (block nesting is unverifiable, so it's a caller contract).
  This is the RPC service-return shape. See the method docstring for the full
  contract + allowed/disallowed shapes; `_build_concurrent_child_bridge` for the
  mechanics.

Block nesting is a per-task runtime property and is **not** statically checked.
See `notes/scope_service_lifetimes.md` for the full picture (mounting, lifespans,
and moving services between scopes), and the method docstring for the contract +
allowed/disallowed shapes.

#### Scope-local caching

Each scope has its own `_ref_loaded_value_cache` for immutable entities.
Cache entries are copied from parent scopes on `lift()`,
`copy_from_scope()`, or `track_from_scope()`, and destroyed when the
scope exits.

#### Scope freezing

`freeze_scope()` freezes a scope, preventing any further mutations to
its tracked node set. All mutating operations (`store`, `load`, `lift`,
`copy_from_scope`, `abandon`, `clear_cache`, `track_builtin`) raise
`RuntimeError` on a frozen scope. Read-only operations
(`get_cached_value`, `is_tracked`, `as_active_scope`) remain allowed.
Freezing requires all parent scopes to already be frozen; root scopes
cannot be frozen. The recommended pattern is to create an isolated
scope, populate it via a helper child scope that also includes the
global scope (for type builtins), then freeze the isolated scope. Child
scopes of a frozen scope are separate mutable scopes.

### Top-level helpers / globals

- **`FxtrScope.get_active()`**: Classmethod returning the
  currently-active scope (recommended API).
- **`ACTIVE_SCOPE`**: The underlying context variable (defaults to
  `GLOBAL_SCOPE`).
- **`GLOBAL_SCOPE`**: The root scope for top-level entities.
- **`GLOBAL_MEMORY_STORE`**: Context variable for the single global
  `MemoryStore` shared by all scopes.
- **`get_active_context_marker()`**: Helper to get the current context
  marker.
- **`track_builtin_id(name)`**: Track a builtin ID in the active scope.

## memory_arena.py

Global memory store with lease-backed lifetime management. All scopes
share a single `MemoryStore` instance. Lifetime tokens come from
`fxtr2.leases.lease_types` — callers mint their own
`UnconditionalLocalLifetime` (or other lifetime type), then register the
token with the store via `register_token`. The store is a *subscriber*:
it allocates a per-token row (reverse index + bridges) and hooks
`_on_token_end` onto the token's end-callback for GC, but holds no
lease on the token itself, caches no grant, and tracks no
parent/ancestor relationships. No explicit invalidation call exists:
when the last lease clone for a token is discarded anywhere in the
system, the token ends, the store's end-callback fires, and cleanup
runs.

### Sync-Dep Closure invariant

Any token with access to a node also has access to its sync-discoverable
deps (`unwraps_to`, `data.ctx_node`, `data.backstep_node`) — recursively.
So a sync query returning a dep ID always returns one the caller can
immediately track. Maintained by routing every gain-of-access through
`_add_owned_token` (direct ownership; **strict-asserts** that T already
has access to every dep — `unwraps_to` plus everything in
`data.requires`, which covers load-time CBOR children for d/n too — so
contract violations fail loudly at the grant site rather than silently
corrupting state) or `_adopt_inheritance_entry` (source-driven adoption;
builds the proof bottom-up via source queries that auto-track each dep
before its inheritance/ownership claim is installed — derived nodes get
**owned**, d/n get **inherited**). Callers are responsible for
establishing dep access before invoking `_add_owned_token`: bottom-up
recursive sub-stores for fresh *and dedup'd* n/p store paths (the n/p
cases of `_bare_store_impl` unconditionally store their unwrap
projection first, which also repairs a dataless d-form left behind by a
foreign load before ownership is granted), deps-first worklists for
batch transfers, etc. See the `MemoryStore` class docstring for the
full statement and bound argument.

### Types

- **`LifetimeToken`**: Imported from `fxtr2.leases.lease_types` (no
  longer defined here). The store treats every token as a fully
  independent storage layer; lifetime ordering between scopes is
  enforced by the scopes' own ancestor cap-bridges.

- **`MemoryStoreNode`**: Unified node type. Tracks:
  - `data: NodeData | None` — in-memory bytes and dep info. Always
    populated for derived tags (`./!/c/p/u`) once any token has access;
    may be None for d/n nodes whose entity bytes haven't been
    async-loaded yet.
  - `unwraps_to: FxtrID | None` — provenance chain (always available,
    even without data).
  - `inheritance_bridges: dict[X_reg, _InheritanceEntry]` — per-token
    inheritance entries. Each entry's `source` is either a foreign
    `ReadableArena[Y]` or a `_SameStoreSentinel(target_token=Y)`. The
    shared bridge record (`LocalBridge`, `claimed_ids`, break-callback
    bookkeeping) for each `(X_reg, Y)` pair lives in
    `_token_data[X_reg].bridges[Y]`.
  - `owned_tokens: set[LifetimeToken]` — tokens with direct ownership
    (data + every dep covered).

- **`_SameStoreSentinel`**: Inheritance-source marker for the case
  where the source is a `MemoryArena` bound to this same `MemoryStore`.
  Carries the target token; loads through the sentinel recurse via
  `_ensure_own` under that token rather than calling the source arena's
  `load_flat`.

- **`_BridgeRecord`**: Per-`(X_reg, Y)` store-side record: `bridge:
  LocalBridge` (the `IndependentLocalBridge | DependentLocalBridge`
  whose at-rest leases pin Y alive while X_reg is alive), `claimed_ids`
  (IDs backed by this bridge under X_reg), `deregister_callbacks` (one
  per at-rest lease, called on intentional teardown), `break_reason`
  (set on break; re-surfaced on subsequent loads).

- **`LifetimeTokenData`**: Per-token row on `MemoryStore`:
  `node_associations` (reverse index of every node where this token
  appears in `owned_tokens` or `inheritance_bridges`) and `bridges:
  dict[Y, _BridgeRecord]` (this token's bridge records). The store does
  *not* cache the token's ULL or its grant (subscriber role).

### MemoryStore (global backing store)

- Per-token row: `_token_data: dict[LifetimeToken, LifetimeTokenData]`.
- Arenas: `new_arena(lease)`; internal `_new_arena_for_token(token)`
  allocates another empty arena for an already-registered token (e.g.
  tests). Arena state (liveset) lives in `_arenas[key]:
  MemoryArenaState`; `weakref.finalize` machinery logs leaks and
  auto-closes on GC.
- Token lifecycle: `register_token(token, *, name=...)` subscribes the
  store to a caller-owned lifetime. Allocates an empty
  `LifetimeTokenData` row and hooks `_on_token_end` onto the token's
  end-callback. It holds no lease on the token itself; the caller's own
  root lease drives the lifetime. Duplicate registration of the same
  token raises.
- Lease validation: `_take_and_validate(lease)` checks that the lease's
  subject token is registered and still active, then consumes the
  lease. Grant-level checking is not performed (subscriber role).
- Bridge management: `_register_bridge(x_reg, y, bridge)` (idempotent on
  `(x_reg, y)`; takes ownership of every at-rest lease and registers
  broken-callbacks), `_invoke_bridge(x_reg, y, base_lease)` (per-call
  mint of `Lease[Y]` — `base` is taken; an `IndependentLocalBridge`
  clones its stored lease, a `DependentLocalBridge` runs its converter
  on cloned input leases plus the base), `_release_bridge_claim(x_reg,
  y, node_id)` (drops the node from the record's claim set; tears down
  the bridge if empty).
- Node lifecycle: `bare_store(lease, entity)`, `bare_store_flat(lease,
  flat_data)`, `bare_load(lease, node_id)`, `bare_load_flat(lease,
  node_id)`, `bare_copy_from_arena(dest_lease, source, src_lease,
  entity_id)` (DAG copy; no liveset update).
- Inheritance: `bare_lazily_attach_from_arena(lease, node_ids, source,
  bridge: LocalBridge)` — auto-detects same-store sources (sentinel
  mode skips the `copy_to_new_arena` fork), registers the `(X_reg, Y)`
  bridge record idempotently, and adopts each node via the bridge.
- Deep-fetch lookup: `try_lookup_with_lease(lease, node_id)` returns a
  fresh `MemoryArena[B]` tracking the entity if the caller's token has
  access (owned or inheritance entry).
- GC: token-based via end-callback. `_on_token_end` walks
  `node_associations`, drops the token from every node's claim maps,
  tears down the token's bridges, and frees nodes whose maps both
  become empty.
- Sync transfer: `bare_track_from_memory_arena(dest_lease, src_lease,
  entity_id, *, dest_inheritance_bridges)` walks the entity DAG
  bottom-up via a discover/save worklist, mirroring
  `bare_copy_from_arena`'s shape but without async I/O. For each node
  it picks the cheapest path: skip (dest already covers); **claim-holder
  adopt** — if any token in `dest_inheritance_bridges` holds a claim
  (owned or inherited) on the node, install a `_SameStoreSentinel`
  entry targeting that holder (no data, no I/O; sound because the
  dest's bridge keeps the holder alive and claims only drop at token
  end), stopping the walk there with `d`/`n` deps left lazy; or grant
  `dest_token` ownership and recurse on deps. Derived nodes always have
  populated data per the Sync-Dep Closure invariant; `d`/`n` nodes
  without in-memory data and without a dest-bridged claim-holder fail
  (would require an async load). When the source token is itself
  dest-bridged (marker-inherit / lift-style calls) the root matches
  immediately and the walk degenerates to a lazy adopt.

### MemoryArena (view into the store)

A `MemoryArena[B]` is a "view" into the store, parameterized by a
lifetime brand. Tracked node IDs live in `MemoryStore._arenas[key].liveset`
(`MemoryArenaState`). The arena caches its token identity only (no
grant cache — same subscriber rationale as the store); every public
method calls `_precheck_lease_for_arena` (liveness +
subject-matches-token) before consuming the lease.

- `lazily_attach_from_arena(lease, node_ids, source, bridge)`: Attach
  entities from a source arena via a `LocalBridge`; same-store sources
  are detected automatically and stored as sentinels.
- `copy_from_arena(lease, source, src_lease, entity_id)`: Copy entities
  from another arena (unified DAG-walk logic).
- `copy_to_new_arena(lease, node_ids)`: Fork a new arena with the same
  token + grant containing specified entities; no new lifetime
  introduced.
- `load_flat(lease, node_id)`: Load entities without deserializing,
  returning raw bytes and dependencies.
- `track_from_memory_arena(lease, source, src_lease, entity_id, *,
  dest_inheritance_bridges={})`: Sync DAG walk that extends this
  arena's claim over the entity. Same-store source required. The
  optional `dest_inheritance_bridges` lets the walk lazily adopt nodes
  claim-held by any of the bridged tokens on the way down (typically
  the destination scope's `is_ancestor_or_arena_owner=True`
  cap-bridges).
- `MemoryStore.track_in_arena(node_id, key)` /
  `untrack_from_arena(lease, node_id, key)` — internal helpers for
  liveset membership; the leased variant validates the lease's token
  against the arena row.

## fxtr_context_api.py

User-facing API for working with persistent namespaces:

- **`FxtrNamespace`**: Abstract base for namespace operations (pin,
  unpin, retrieve).
  - Each namespace has its own `scope` property for managing entity
    lifetimes.
  - `_resolve_into_scope()`: Helper for validating and resolving target
    scopes for output operations.
- **`FxtrQueryableNamespace`**: Extends `FxtrNamespace` with indexing
  and querying support.
  - `register_indexer()`: Register an indexer (Indexer | FxtrID |
    EntityRef) at a key prefix.
  - `unregister_indexer()`: Unregister an indexer at a key prefix.
  - `list_indexers()`: List registered indexers, returning
    `EntityRef[WellKnownBuiltinIndexer]` refs.
  - `iter_matches()`: Query indexes with `IndexerConstraint` objects,
    yielding `(key, entity_id, bindings)` tuples.
  - `sql()`: Run arbitrary analytical SQL queries against named
    `QueryView`s. Each view becomes a CTE composed of joined indexer
    constraints. Executes in an isolated read-only connection (ATTACH +
    in-memory DB). Returns `list[sqlite3.Row]`.
  - Input pattern follows `pin()` (resolve entity from scope); output
    pattern follows `retrieve()` (attach to namespace scope, then
    lift/copy to target scope).
- **`SQLiteFxtrNamespace`**: SQLite-backed namespace extending
  `FxtrQueryableNamespace`, used as an async context manager.
  - `isolate_scope=True` creates an isolated scope for the namespace.
  - `read_only=True` opens the store in read-only mode: no session is
    tracked, no GC runs, no session files are created, and
    pin/unpin/register_indexer/unregister_indexer raise `RuntimeError`.
    SQLite connections use `mode=ro` and no write pool is created.
    Read-only sessions are fully invisible to other processes.
- **`TsvFxtrNamespace`**: TSV-file fxtr namespace (pin mappings in git-tracked
  TSV files, backed by a repo-level entity store) extending
  `FxtrQueryableNamespace`, used as an async context manager.
