# Stage 2p3 — Sandbox provenance

## Goal

Attach a provenance context to every entity / service produced inside a
sandbox guest, so that cross-sandbox computations can be traced back to
(a) the bundle that spawned the guest and (b) the `connect()` config that
initialized it — including the provenance of any services bridged in via
that config.

This is the first user of `FxtrScope(provenance_context=...)` and the
`$fxtr.ctx` framework method in the sandboxing layer. Stage 9 of fxtr2
shipped the scope-level provenance plumbing and the wire-level
prov-aware FxtrID encoding; we're consuming it here.

## End-user semantics

Before this change: when a sandbox guest stores an entity or mounts a
service, the resulting `FxtrID` has no `_provenance_parent`. There's no
way to tell from the ID alone that the entity came from a sandboxed
computation.

After this change: every entity / service the guest produces carries a
`_provenance_parent` chain that resolves to a c-node whose payload
describes:

- The bundle entity (`TaskBundle` / `DockerTaskBundle` /
  `PyContainerTaskBundle`) used to spawn the guest.
- An immutable snapshot of the `connect()` config arg, with each
  `ServiceRef` leaf replaced by a `ServiceConfigReplacementEntity` that
  records the service's `$fxtr.type` and `$fxtr.ctx` (so the captured
  trace continues into whatever spawned that service).

The provenance context is **per-`connect()`-call**, because services in
config can vary per call.

## Background

- **Stage 9 / FxtrScope `provenance_context`.** `FxtrScope(...,
  provenance_context=c_node)` sets the scope's `$fxtr.ctx` marker to
  `FxtrID(".", _provenance_parent=c_node)`. Once set, every
  `store_cbor` / `load_cbor` / `lift` / `copy` op round-trips
  provenance automatically (see
  `v2/plans/phase3/stage9_provenanced_scopes/`). The wire layer
  (Stage 9 / Stage 3) ships layered FxtrIDs over service calls
  transparently — server-side decode uses
  `embed_as_provenance_parent` so prov flows in both directions.
- **`$fxtr.ctx` framework method.** Every service impl based on
  `CommonServiceImpl` answers `$fxtr.ctx` (no args, returns the mount
  scope's `get_context_marker_id()`) and `$fxtr.type` (returns the
  impl's `_cached_service_type_id`). On the untyped path callers reach
  these via `await getattr(proxy, "$fxtr.ctx")()`.
- **Sandbox connect flow** (`projects/fxtr_sandboxing/CODE_OVERVIEW.md`,
  *Spawn → connect*). Host calls `spawner.spawn(bundle)`, then
  `replace_service_type(SandboxConnectProto[T, U])`, then
  `connect(config)`. Each `connect` on the guest side dispatches into
  `_GuestMainImpl.connect`, which runs the user entrypoint inside
  the per-call dispatch scope (`call_scope`).

## High-level design

Two new entities live in `fxtr_sandbox_types/entities.py`:

- `ServiceConfigReplacementEntity` — sentinel that replaces a
  `ServiceRef` leaf in the captured config snapshot. Carries the
  service's `$fxtr.type` and `$fxtr.ctx` FxtrIDs.
- `SandboxProvenanceContext` — d-node payload that gets c-node-wrapped
  to form the guest scope's provenance context. Holds the bundle ref,
  the config snapshot (services replaced), and a `value` field
  defaulting to `<FxtrID .>` so the c-node unwraps cleanly.

The protocol surface gains a second protocol:

- `SandboxConnectProto[T, U]` — **unchanged user-facing surface.** What
  host code resolves and calls `connect()` on.
- `SandboxGuestImplProto[T, U]` — **new internal protocol.** What the
  guest-side `_GuestMainImpl` actually implements. Exposes
  `connect_with_bundle_provenance(config: T, bundle: EntityRef[Any]) ->
  ServiceRef[U]`.

The host-side spawner does the threading:

- Old: `spawn` returns the raw connect ref (which directly speaks
  `SandboxConnectProto`).
- New: `spawn` mounts a small host-side `_SpawnConnectWrapperImpl`
  (implementing `SandboxConnectProto[T, U]`) that holds the raw guest
  ref + the bundle ref, and forwards `connect(config)` to the guest's
  `connect_with_bundle_provenance(config, bundle_ref)`. The wrapper's
  ref is what the user receives.

Guest side, in `connect_with_bundle_provenance`:

1. **Single-pass async walk** of `config` (FxtrServiceData tree).
   Each leaf is handled in one place:
   - `ServiceRef`: invoke `$fxtr.ctx` + `$fxtr.type`, build a
     `ServiceConfigReplacementEntity(service_type_id=...,
     service_ctx=...)`, `store()` it, emit a
     `RawStrongFxtrID(replacement_id)` into the snapshot.
   - `EntityRef`: `await ref.load()` as `UnknownEntity`, reject with
     a wire error if its `$fxtr.type` equals
     `SERVICE_CONFIG_REPLACEMENT_TYPE_ID` (escape-safety check);
     otherwise emit a `RawWeakFxtrID(ref.entity_id)`.
   - `FxtrID`: emit `RawWeakFxtrID(fid)`.
   - primitives / Mapping / Sequence: passthrough / recurse.

   Replacements minted during the walk are stored directly into the
   snapshot and never re-walked, so there's nothing to dedupe between
   the safety check and the replacement step.
2. Store `SandboxProvenanceContext(bundle=bundle_ref,
   config_snapshot=<walked snapshot>)` via mashumaro:
   `prov_entity_ref = SandboxProvenanceContext(...).store()`. The
   default `value=FxtrID.builtin(".")` fires.
3. Mint a c-node from the resulting d-node. We need a scope-level
   `create_c_node` that takes the entity's d-node id. v2 has
   `MemoryArena.create_c_node(lease, context_data: FxtrCbor)` but no
   scope-level wrapper. Add `FxtrScope.create_c_node` following the
   pattern of `FxtrScope.c_node_to_context_data` /
   `FxtrScope.create_u_node` — see *v2 additions* below.
4. **Override `call_scope`'s context marker** to
   `FxtrID(".", _provenance_parent=c_node)` via
   `call_scope._apply_context_marker_override(provenance_context=c_node,
   provenance_context_from_scope=call_scope)`. From this point on,
   anything stored / mounted in `call_scope` (or its children) carries
   the c-node as `_provenance_parent`.

   This is a private-method call into v2. We deliberately stay
   non-invasive (no v2 changes other than the additive
   `scope.create_c_node`) and mark with a TODO; see the *Sharp edges*
   section.
5. Decode the original `config` against the entrypoint's first-param
   annotation (existing behavior), call the user entrypoint, return
   the resulting `ServiceRef`.

Because `call_scope`'s context marker now has a `_provenance_parent`,
`store_cbor` runs the provenance projection path (see
`v2/fxtr2/data/entity_ref_scope.py:2866`). The user impl's mount, any
entities it stores, and any sub-call dispatch scopes all inherit
the c-node marker.

## New entities

In `fxtr_sandbox_types/entities.py`:

### `ServiceConfigReplacementEntity`

```text
ServiceConfigReplacementEntity (DataclassEntity)
  fxtr_type = define_type(SHARED_SANDBOX_NS, "service_config_replacement")
  service_type_id: FxtrID
  service_ctx: FxtrID
```

Always represents a leaf in the captured config snapshot. The two
fields capture the service's identity (`$fxtr.type`) and its mount
scope's provenance marker (`$fxtr.ctx`); the marker carries its own
`_provenance_parent` chain when applicable, which flows automatically
through the provenance-aware `store_cbor` path.

`DataclassEntity` (not `DataclassFxtrSubpart`) because the snapshot
tree is arbitrarily-shaped untyped `FxtrServiceData`-style cbor with
no surrounding dataclass schema to anchor a subpart annotation; using
a top-level entity with its own `$fxtr.type` discriminator lets us
substitute it at any depth.

Define `SERVICE_CONFIG_REPLACEMENT_TYPE_ID` alongside the class for the
escape-safety check.

### `SandboxProvenanceContext`

```text
SandboxProvenanceContext (DataclassEntity)
  fxtr_type = define_type(SHARED_SANDBOX_NS, "sandbox_provenance_context")
  bundle: EntityRef[TaskBundle | DockerTaskBundle | PyContainerTaskBundle]
  config_snapshot: FrozenFxtrCbor
  value: FxtrID = field(default=FxtrID.builtin("."))
```

The `value` field is required for c-node creation (`create_c_node`
expects the d-node payload to have a `value` referencing what the
c-node unwraps to).

`config_snapshot: FrozenFxtrCbor` — this represents the post-walk
snapshot directly in CBOR form (immutable, frozendict-shaped). Why
not `Any`: `FrozenFxtrCbor` enforces the right structural constraint
and the cbor leaves (`RawWeakFxtrID`, `RawStrongFxtrID`,
`RawFxtrEntityMarkerID`) are exactly what the walker produces. Why
not `FxtrServiceData`: that type-alias includes `ServiceRef`, which
we've eliminated from the snapshot.

Mashumaro-via-`DataclassEntity` is expected to handle this directly:
`FrozendictSerializationStrategy` is already registered, mashumaro
passes unknown leaf objects through unchanged, and the raw-id
wrappers (`RawWeakFxtrID` / `RawStrongFxtrID` /
`RawFxtrEntityMarkerID`) are interpreted by `fxtr_cbor_dumps` at
the cbor layer, not by mashumaro. If serialization unexpectedly
fails at implementation time, pause and reassess — most likely a
small additional strategy on `FrozenFxtrCbor` is all that's needed.

## New protocol

In `fxtr_sandbox_types/protocols.py`:

```python
SANDBOX_GUEST_IMPL_TYPE_ID = system_entities.define_type(
    SHARED_SANDBOX_NS, "sandbox_guest_impl"
)

@service_protocol
class SandboxGuestImplProto[T, U](Protocol):
    """Internal protocol the guest-side _GuestMainImpl exposes.

    Wrapped on the host side by a _SpawnConnectWrapperImpl that
    implements SandboxConnectProto[T, U] and threads in the bundle
    ref. End-user code never sees this protocol — host wrapper
    bridges in the bundle and the user-facing surface stays
    `connect(config)`.
    """
    async def connect_with_bundle_provenance(
        self,
        config: T,
        bundle: EntityRef[TaskBundle | DockerTaskBundle | PyContainerTaskBundle],
    ) -> ServiceRef[U]: ...
```

`SandboxConnectProto` is unchanged.

## Host-side changes

### `_SpawnConnectWrapperImpl` (new)

In `fxtr_sandboxing/spawner.py`, alongside `SandboxSpawnerImpl`:

```text
class _SpawnConnectWrapperImpl(TypedServiceImpl[SandboxConnectProto[FxtrServiceData, UnknownService]]):
    def __init__(self, raw_guest_ref, bundle_ref):
        self._raw_guest_ref = raw_guest_ref   # ServiceRef[SandboxGuestImplProto[...]]
        self._bundle_ref = bundle_ref         # EntityRef[...]
    _target_type_annotation -> SandboxConnectProto[FxtrServiceData, UnknownService]
    _service_type_id -> SANDBOX_CONNECT_TYPE_ID
    @service_method
    async def connect(self, config):
        proxy = await self._raw_guest_ref.resolve()
        return await proxy.connect_with_bundle_provenance(config, self._bundle_ref)
```

Captured-state: `_raw_guest_ref` keeps the underlying guest connection
alive through the wrapper's lifetime; `_bundle_ref` rides the same
way. Both must remain reachable in scopes the wrapper's dispatch can
see — straightforward given the wrapper is mounted on the spawn-call
scope (where both refs are already tracked, see below).

### `SandboxSpawnerImpl.spawn` rewiring

Today:

```text
spawn(bundle):
    loaded = await bundle.load()
    runner = <one of three>
    connect_ref, session_scope = await env_tg.start(runner, loaded, teardown_event)
    bridge connect_ref into call_scope
    return connect_ref.replace_service_type(...)
```

New:

```text
spawn(bundle):
    loaded = await bundle.load()
    runner = <one of three>
    raw_guest_ref, session_scope = await env_tg.start(runner, loaded, teardown_event)
        # raw_guest_ref now typed as SandboxGuestImplProto[FxtrServiceData, UnknownService]
    bridge raw_guest_ref into call_scope (active scope at spawn dispatch)
    wrapper_ref = call_scope.mount(_SpawnConnectWrapperImpl(raw_guest_ref, bundle))
    return wrapper_ref.replace_service_type(
        SandboxConnectProto[OverrideThisTypeBeforeUsing, OverrideThisTypeBeforeUsing]
    )
```

Both `raw_guest_ref` and `bundle` (the arg `EntityRef`) are
already tracked in the dispatch call_scope (the framework bridges
service-call args into it). Mounting the wrapper on call_scope is
correct: when the user releases the wrapper ref, call_scope drains,
the wrapper unmounts, its captured `raw_guest_ref` releases its
chain-bridge to `session_scope`, the RPC connection drains, the
guest tears down. Same lifetime chain as before; we've just inserted
one local hop.

The three `_spawn_runner_*` methods change their `remote_service_type`
arg in `open_rpc_scope` from
`SandboxConnectProto[FxtrServiceData, UnknownService]` to
`SandboxGuestImplProto[FxtrServiceData, UnknownService]`.

## Guest-side changes

### `_GuestMainImpl` retyped

In `fxtr_sandbox_rpc_adapter/_guest_main.py`:

- Class is now
  `TypedServiceImpl[SandboxGuestImplProto[FxtrServiceData, UnknownService]]`.
- `_target_type_annotation` returns
  `SandboxGuestImplProto[FxtrServiceData, UnknownService]`.
- `_service_type_id` returns `SANDBOX_GUEST_IMPL_TYPE_ID`.
- Single method `connect_with_bundle_provenance(config, bundle)`
  replaces today's `connect(config)`. See body below.

### Provenance setup body

```text
connect_with_bundle_provenance(config, bundle):
    call_scope = get_active_scope()

    # 1. Single async walk: replace services AND reject SCRE-smuggling
    config_snapshot = await _walk_config_for_provenance(config)

    # 2. Build + store SandboxProvenanceContext via mashumaro
    prov_ref = SandboxProvenanceContext(
        bundle=bundle,
        config_snapshot=config_snapshot,
    ).store()

    # 3. Mint c-node from the d-node id
    c_node = call_scope.create_c_node_from_entity(prov_ref)
        # see "v2 additions" — exact shape decided at impl time

    # 4. Override call_scope's context marker (private call; TODO sub-scope)
    call_scope._apply_context_marker_override(
        provenance_context=c_node,
        provenance_context_from_scope=call_scope,
    )

    # 5. Decode + invoke entrypoint inside call_scope (now provenanced)
    decoded = decode_from_service_data(config, self._user_config_annotation)
    return await self._entrypoint(decoded)
```

### Walker — `_walk_config_for_provenance`

Lives in a new module `fxtr_sandbox_rpc_adapter/_provenance.py`. Async,
single-pass. Walks any `FxtrServiceData` tree, doing both the
ServiceRef → replacement substitution **and** the EntityRef
escape-safety check in the same traversal:

- `None | bool | int | float | str | bytes` → leaf, returned verbatim.
- `FxtrID` → emit `RawWeakFxtrID(fid)`.
- `EntityRef[T]` → `await ref.load()` as `UnknownEntity`, reject
  (`IRException` / similar wire error) if loaded type id ==
  `SERVICE_CONFIG_REPLACEMENT_TYPE_ID`. Otherwise emit
  `RawWeakFxtrID(ref.entity_id)`. The Python target-type annotation
  is dropped; the ID is what carries provenance.
- `ServiceRef[T]` → invoke `$fxtr.ctx` + `$fxtr.type` on the bridged
  proxy (via the untyped `getattr(proxy, "$fxtr.*")()` path), build
  `ServiceConfigReplacementEntity(service_type_id=...,
  service_ctx=...)`, `.store()` it (active scope is call_scope),
  emit a `RawStrongFxtrID(replacement_ref.entity_id)` in the
  snapshot. Strong link because the replacement is logically owned
  by the snapshot.
- `Mapping[str, ...]` → `frozendict` with recursed children.
- `Sequence` (non-str / non-bytes) → `tuple` with recursed children.
- Anything else → wire-error (defense-in-depth; the typed dispatch
  already validated FxtrServiceData server-side before reaching
  `_handle_invoke`).

Returns a `FrozenFxtrCbor`-typed result.

**Parallelism**: independent leaves (multiple ServiceRefs / EntityRefs
in sibling positions) can be fanned out via `anyio.create_task_group`
+ result slots; ordering is preserved by the tree-shape walk. Plan to
implement the simple sequential version first; introduce a
task-group fanout if/when call-side latency from many refs in one
config matters.

**No tracking of "ours vs theirs"**: replacements are minted *during*
the walk and emitted directly into the output; they're never
re-visited by the walker. The escape-safety check applies only to
user-provided `EntityRef` leaves found in the input tree.

**Top-level only**: the walk inspects FxtrServiceData tree shape;
nested entity contents aren't loaded or recursed. A user wrapping a
`ServiceConfigReplacementEntity` inside another DataclassEntity
they own bypasses the check — counted as deliberate opt-in.

### Building the cbor + minting the c-node

We want the cbor of a `SandboxProvenanceContext` instance to hand to
`scope.create_c_node(cbor)` — and we want it without double-storing
the d-node (and without sync-reading it back from the arena, which
has subtle invariants).

The clean shape: split the existing `DataclassEntity._fxtr_store_impl`
into two methods (refactor):

- `_fxtr_convert_to_cbor(self) -> FxtrCbor` — does the cbor-build
  steps: `to_dict()` under the `RecursiveStore` context, plus the
  top-level `$fxtr.ctx` sentinel replacement against the active
  scope's marker.
- `_fxtr_store_impl(self) -> EntityRef[Self]` — calls
  `_fxtr_convert_to_cbor()`, then `scope.store_cbor(cbor)` (with the
  existing `DuplicateEntityMarkerError` note-adding behavior), then
  builds the `EntityRef` with the inferred default type-params.

For sandbox provenance, we then do:

```text
cbor = SandboxProvenanceContext(
    bundle=bundle,
    config_snapshot=config_snapshot,
)._fxtr_convert_to_cbor()
c_node = call_scope.create_c_node(cbor)
```

`scope.create_c_node` internally stores the d-node from the cbor and
mints the c-node. We get a tracked c-tagged FxtrID; no double-store,
no arena read-back, all routine API.

## v2 additions

Three small changes in v2:

**`v2/fxtr2/data/entity_ref_scope.py`** — `FxtrScope.create_c_node`,
matching the pattern of `create_u_node` / `c_node_to_context_data`:

```python
def create_c_node(self, context_data: FxtrCbor) -> FxtrID:
    """Mint a c-node wrapping the given d-node payload.

    Thin scope-level wrapper over MemoryArena.create_c_node. The
    payload must have a "value" field; the resulting c-node unwraps
    to that value. Returned ID is bare c-tagged; caller can pass it
    as a scope's provenance_context or to embed_as_provenance_parent.
    """
    self.check_alive()
    self._check_not_frozen_and_not_tracing_loads("create_c_node")
    return self._main_arena.create_c_node(self._main_lease.clone(), context_data)
```

**`v2/fxtr2/conversion/converters/mashumaro_entities.py`** — refactor
`DataclassEntity._fxtr_store_impl` to extract `_fxtr_convert_to_cbor`:

```python
def _fxtr_convert_to_cbor(self) -> FxtrCbor:
    """Build the entity's CBOR form, applying the $fxtr.ctx override.

    Pure conversion — no storage. Used by ``_fxtr_store_impl`` as
    the first half of its work, and by callers that want the CBOR
    of a mashumaro-shaped entity to hand to alternative storage
    paths (e.g. ``scope.create_c_node``).
    """
    state = RecursiveStore()
    token = RECURSIVE_SERDES_STATE.set(state)
    try:
        cbor = self.to_dict()
    finally:
        RECURSIVE_SERDES_STATE.reset(token)
    if isinstance(cbor.get("$fxtr.ctx"), RawFxtrEntityMissingMarkerSentinel):
        cbor["$fxtr.ctx"] = RawFxtrEntityMarkerID(
            inner_id=get_active_scope().get_context_marker_id()
        )
    return cbor

def _fxtr_store_impl(self) -> EntityRef[Self]:
    cbor = self._fxtr_convert_to_cbor()
    store_type = _infer_default_type_params(type(self))
    scope = get_active_scope()
    try:
        fid = scope.store_cbor(cbor)
    except DuplicateEntityMarkerError as exc:
        exc.add_note(...)  # existing Child[T] hint, unchanged
        raise
    return EntityRef(fid, store_type)
```

Both changes are additive / non-behavior-changing for existing
callers. Add corresponding `v2/CODE_OVERVIEW.md` +
`v2/fxtr2/data/LOCAL_CODE_OVERVIEW.md` updates as part of the
change.

**`v2/fxtr2/data/entity_ref_scope.py`** —
`_track_provenance_context_from` lease-source fix. The original
implementation always cloned `source_scope._main_lease`, which fails
when the source is a parent scope in late-life state (alive via
chain-bridges, at-rest `_main_lease` discarded). Surfaced when a
service mounted in a provenance-bearing scope has a subsequent
method call dispatched — the child dispatch scope inherits the
marker, walks parents, tries to clone the parent's
already-discarded `_main_lease`.

The fix mirrors the `_lift_unchecked` pattern: pass the source-side
lease explicitly. Three callers in `_apply_context_marker_override`:

- **Parent-inference** (caller passes `provenance_context=None`):
  mint via `self._mint_ancestor_lease(pscope._token)`. Works even
  when `pscope._main_lease` is gone, because `self`'s ancestor
  bridges hold their own at-rest clone of the parent's main token
  for `self`'s full lifetime.
- **Implicit source from parent walk** (`provenance_context` given,
  `provenance_context_from_scope=None`): same minting pattern.
- **Explicit `provenance_context_from_scope`**: keep
  `source_scope._main_lease.clone()` — the caller has chosen the
  source and is responsible for it being alive.

## Escape-safety: pre-call rejection

`_reject_replacement_smuggling` happens **before** we call
`_apply_context_marker_override` and before user entrypoint invocation.
If smuggling is detected, raise an `IRException(category=...)`-style
error — wire-level so the host sees a clean failure.

## File-level changes

```
v2/fxtr2/data/
  entity_ref_scope.py       # + FxtrScope.create_c_node (additive)
                            # + _track_provenance_context_from(source_lease=) +
                            #   ancestor-lease minting in _apply_context_marker_override
                            #   (lease-discard fix for parent-late-life inheritance)
v2/fxtr2/conversion/converters/
  mashumaro_entities.py     # refactor _fxtr_store_impl: extract _fxtr_convert_to_cbor
                            # + FrozenFxtrCborSerializationStrategy (thaw/freeze)

projects/fxtr_sandboxing/
  fxtr_sandbox_types/
    fxtr_sandbox_types/
      entities.py           # + ServiceConfigReplacementEntity (+ SERVICE_CONFIG_REPLACEMENT_TYPE_ID)
                            # + SandboxProvenanceContext
                            # + DirectLauncherSentinel (placeholder bundle for direct_launcher)
      protocols.py          # + SandboxGuestImplProto + SANDBOX_GUEST_IMPL_TYPE_ID
      __init__.py           # re-export new symbols

  fxtr_sandbox_rpc_adapter/
    fxtr_sandbox_rpc_adapter/
      _guest_main.py        # _GuestMainImpl retyped to SandboxGuestImplProto;
                            #   connect → connect_with_bundle_provenance with provenance body
      _provenance.py  (NEW) # single-pass async walker (substitution + escape-safety check)

  fxtr_sandboxing/
    spawner.py              # + _SpawnConnectWrapperImpl; spawn() rewires through wrapper;
                            #   _spawn_runner_* methods use SandboxGuestImplProto remote type
```

Three example task bundles (rpc_task, transport_task, nested_relay_task,
and their container/py_container siblings) are unchanged — they use the
user-facing `SandboxConnectProto[T, U]` and don't care that there's
a wrapper hop.

## Tests

New file: `projects/fxtr_sandboxing/tests/test_sandbox_provenance.py`. Covers:

1. **Smoke**: spawn the existing `rpc_task` WASM bundle, connect with a
   plain config, call `greet`. Verify the returned `ServiceRef`'s
   `$fxtr.ctx` is a layered `FxtrID(".", _provenance_parent=c_node)`,
   and that the c-node unwraps to a `SandboxProvenanceContext` whose
   `bundle` field matches the bundle ref we spawned.
2. **Service-in-config replacement**: spawn `nested_relay_task` (which
   takes an authorizer ref). Connect with a config containing a
   `ServiceRef[NetworkHandleAuthorizerProto]`. Verify the c-node's
   d-node payload has a `ServiceConfigReplacementEntity` at that
   position whose `service_type_id` matches
   `NETWORK_HANDLE_AUTHORIZER_TYPE_ID` and whose `service_ctx`
   matches the authorizer's mount-scope marker.
3. **Provenance flow into stored entities**: have the user entrypoint
   `store()` an entity and return its ref. Verify the returned ref's
   `entity_id._provenance_parent` is set and unwraps to a chain that
   eventually lands at the c-node.
4. **Escape-safety**: construct an `EntityRef` to a
   `ServiceConfigReplacementEntity` on the host side (deliberately
   wrong — pretending it's a config arg). Spawn + connect with it
   directly as the config. Verify the call raises
   `IRException` (or similar) on the host side before the user
   entrypoint runs.
5. **Escape-safety bypass via wrapping**: wrap the
   `ServiceConfigReplacementEntity` inside an outer DataclassEntity
   the user controls. Verify the call succeeds (we don't recurse).
6. **Per-connect provenance**: two `connect` calls on the same spawn
   with different configs. Verify each user-returned ref has a
   *different* c-node, both bundled-identical but with different
   `config_snapshot` fields.
7. **Container parity**: minimal repeat of (1) with
   `container_rpc_task` / `py_container_rpc_task`. Behind the
   container-runtime skip marker.

## CODE_OVERVIEW.md updates

After implementation:

- `projects/fxtr_sandboxing/CODE_OVERVIEW.md` — describe
  `SandboxGuestImplProto` and the wrapper hop in *Spawn → connect*;
  describe the provenance setup in a new *Sandbox provenance* section.
  Add `_provenance.py` to the file listing; add the two new entities
  to *Module details / entities.py*.
- `v2/CODE_OVERVIEW.md` + `v2/fxtr2/data/LOCAL_CODE_OVERVIEW.md` —
  note the new `FxtrScope.create_c_node` helper (and
  `create_c_node_from_d_node` if we ship it) alongside the existing
  c-node introspection methods.

## Sharp edges / TODOs

1. **Private-method call into `FxtrScope._apply_context_marker_override`.**
   We override `call_scope`'s context marker post-construction. v2
   doesn't expose a public API for this. The cleaner long-term shape
   is a sub-scope `user_scope = FxtrScope(provenance_context=c_node)`
   inside `call_scope`, but the chain-bridge from "mount in a deeper
   scope than the dispatch scope" → host-caller path hasn't been
   validated. Plan to revisit once that pattern has explicit
   support in v2 / a working test. Mark the call site with a TODO
   referencing this plan.

2. **`FrozenFxtrCbor` field type + mashumaro.** Plan assumes default
   mashumaro handling works (`Frozendict` strategy is already
   registered; raw-id wrappers are leaf passthroughs that
   `fxtr_cbor_dumps` interprets, not mashumaro). If serialization
   fails at impl time, pause + reassess — likely a small
   `FrozenFxtrCbor`-typed strategy on the dialect is the fix.

3. **Service `$fxtr.ctx` round-trip cost.** For each `ServiceRef` in
   config, we round-trip twice (`$fxtr.ctx`, `$fxtr.type`). Sandbox
   configs are typically small (handful of refs) so this is fine; if
   a guest ever needs hundreds of refs we'd want a batched
   introspection method. Punt.

4. **Bundle-load cost.** The host already does
   `await bundle.load()` in `spawn` for dispatch. The bundle ref we
   thread into the wrapper is the same `EntityRef[...]` — no
   additional load on the guest side (we capture identity, not
   content; the guest sees `bundle.entity_id` flowing into the c-node
   payload but doesn't need to deserialize the bundle).

5. **Replacement entity collisions across connects.** Two `connect`
   calls hitting the same `(service_type_id, service_ctx)` pair
   produce identical `ServiceConfigReplacementEntity` IDs (content-
   addressed). That's a feature: replays of the same config get the
   same provenance.

## Implementation steps

1. Add `FxtrScope.create_c_node` in `v2/fxtr2/data/entity_ref_scope.py`;
   add a small unit test in `v2/tests/` against an isolated scope.
   Update `v2/CODE_OVERVIEW.md` + `v2/fxtr2/data/LOCAL_CODE_OVERVIEW.md`.
1a. Refactor `DataclassEntity._fxtr_store_impl` in
    `v2/fxtr2/conversion/converters/mashumaro_entities.py` to extract
    `_fxtr_convert_to_cbor`. Existing
    `DuplicateEntityMarkerError` note-adding stays on the store path.
    Confirm full pre-existing test suite still passes (behavior-
    preserving refactor); add a small targeted test that
    `_fxtr_convert_to_cbor` returns the same cbor that `_fxtr_store_impl`
    would have stored.
2. Add `ServiceConfigReplacementEntity` + type id const +
   `SandboxProvenanceContext` to `fxtr_sandbox_types/entities.py`;
   re-export from `__init__.py`. Spot-check that mashumaro round-trips
   a `SandboxProvenanceContext` with a non-trivial `FrozenFxtrCbor`
   payload; if it doesn't, pause + reassess before continuing.
3. Add `SandboxGuestImplProto` + `SANDBOX_GUEST_IMPL_TYPE_ID` to
   `fxtr_sandbox_types/protocols.py`.
4. Update `projects/fxtr_sandboxing/CODE_OVERVIEW.md` to describe new
   entities and protocol.
5. Add `fxtr_sandbox_rpc_adapter/_provenance.py` with the single-pass
   async walker. Unit-test against a synthetic FxtrServiceData tree
   (mock ServiceRefs that answer `$fxtr.ctx` / `$fxtr.type`).
6. Retype `_GuestMainImpl` to `SandboxGuestImplProto`; rewrite its
   single method to `connect_with_bundle_provenance` with the
   provenance body sketched above.
7. Add `_SpawnConnectWrapperImpl` in `fxtr_sandboxing/spawner.py`;
   rewire `SandboxSpawnerImpl.spawn` to mount it; flip the three
   `_spawn_runner_*` methods to use `SandboxGuestImplProto` as the
   remote service type.
8. Run existing tests (`test_spawner.py`, `test_container_spawner.py`,
   `test_transport_*.py`, `test_nested_spawn_*.py`) and fix any
   regressions from the wrapper hop / retyping. Lifetime + chain-
   bridge bugs likely surface here.
9. Add `tests/test_sandbox_provenance.py` covering the seven scenarios
   above. Land container variants behind the existing skip marker.
10. Update `projects/fxtr_sandboxing/CODE_OVERVIEW.md` to add the test
    summary line + the `_provenance.py` module entry.
