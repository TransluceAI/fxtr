# Substage 6 — `ServiceRef` fundamentals + `UntypedService`

> **Status: working document.** Substage 6 is large enough that we're
> pair-programming it rather than writing a frozen spec. This plan
> captures the design as currently understood; we update it as we go.
> Anything in *italics* is a known unknown waiting to be resolved.

For background see [overview.md §Substage 6](overview.md#substage-6--serviceref-fundamentals--untypedservice)
and [sketch.md](sketch.md). The overview's locked-in decisions still
hold; this doc adds the bridge-taxonomy refinement we landed on
during planning, and describes the dispatch-task structure.

---

## 1. Mental model

### 1.1 The bridge taxonomy

The single most important shift from the overview is splitting
`_CapBridge` into two structurally different cases. Per the
[lifetime conversion mechanics doc](lifetime_conversion_mechanics.md),
holding a `Lease[T]` persistently is only safe when `T` is
*independent of* the holder's lifetime — otherwise we get a cycle.
That's the line we draw:

| Bridge variant | What it stores | Safe to hold past holder's drain? | Used when |
|---|---|---|---|
| **`_IndependentBridge`** | a concrete `Lease[handle.token]` | Yes — `handle.token` doesn't depend on us | The cap was already alive before we existed (e.g. we received its lease as an arg) |
| **`_DependentBridge`** | a `(key_lease, PipelinedLifetimeConverter)` pair | N/A — we mint per-use and drop | The cap depends on us (typical: `handle.token` *is* our token, or a derived child of it) |

`_DependentBridge.converter` has signature
`(Lease[K], Lease[B_self]) -> Lease[B_handle] | UnwrapForPipelining[Lease[B_handle], Any]`
where `B_self` is the bridge-owning scope's main brand. Each lookup mints
a fresh ephemeral `Lease[handle.token]` from `(key.clone(),
self._main_lease.clone())`. Because the minted lease is not held, the
"holding-Lease[T]-pins-our-token" cycle is broken — at rest only the
key clone (on a brand independent of ours) is stored.

**`mount_lowlevel_cap_as_service`** (handle.token == scope's own main
token) becomes a `_DependentBridge` with key=`STATIC_LIFETIME.lease()`
and an identity converter (discards the key, returns `base` unchanged).
We don't store a `Lease[scope.main_token]` because that would cycle.

### 1.2 Scope storage

Two parallel structures:

```python
scope._known_handles: set[IRHandle]
scope._cap_bridges: dict[LifetimeToken, _CapBridgeEntry]

@dataclass
class _CapBridgeEntry:
    bridge: _IndependentBridge | _DependentBridge
    handles: set[IRHandle[Any]]   # subset of _known_handles on this token
    is_ancestor: bool = False
```

`_known_handles` is the flat set of `IRHandle`s this scope is willing
to resolve — kept as a top-level set so `handle in _known_handles` is
O(1). `_cap_bridges` is keyed by `handle.token`; each entry packages
the lease-minting machinery (the inner `_IndependentBridge` /
`_DependentBridge`) with the subset of `_known_handles` that share it.
Multiple handles on the same token share one entry (and therefore one
bridge); tracking the handles per entry lets `forget_service` know in
O(1) whether the bridge is still needed.

`_cap_bridges` carries two kinds of entries:

- **Service entries** (`is_ancestor=False`): the canonical case —
  populated by `mount_*` / `attach_*` / lift; each handle in
  `entry.handles` is a registered ServiceRef target. `forget_service`
  drops empties.
- **Ancestor entries** (`is_ancestor=True`): populated at scope
  construction by `_populate_ancestor_bridges` — one
  `_IndependentBridge(lease=Lease[parent.main_token])` per direct
  parent + transitively from each parent's own ancestor entries.
  These are the scope's *structural ancestor leases* and are the
  source the lift path uses for `base` when invoking dependent
  bridges held by ancestor scopes (so lifts work even when an
  ancestor is mid-drain — its `_main_lease` is gone but our
  ancestor entry's lease is alive for our entire lifetime).
  `forget_service` skips ancestor entries; `_on_token_end` tears
  them down via the same loop.

For now, ancestor entries must be `_IndependentBridge` with a
concrete `Lease`; Dependent / pipelined ancestors are
`NotImplementedError` with a TODO — they'll be needed when
`MemoryStore`'s parent tracking moves fully onto the scope (see
follow-ups).

Resolution = walk `self → self._parents → ...` for the closest scope
that has the handle in `_known_handles`; **closest scope wins**. The
entry for that handle's token lives on the same scope (every
mount/attach/lift registers both together). Tracking handles
separately prevents "magical" cross-resolution: if scope S has only
handle A mounted, an unrelated handle B on the same token cannot be
resolved through S — the parent walk verifies B is registered
somewhere before any local short-circuit kicks in (see §1.5).

Both structures are torn down in the scope-token-end callback (same
pattern as today's `_deep_fetch_sources`); they stay live throughout
drain so in-flight dispatches can still resolve.

`forget_service(ref)` provides early cleanup: removes the handle from
`_known_handles` and from the entry's `handles` set; once the entry's
set empties, drops the entry and discards its leases — *unless*
`is_ancestor=True`, in which case the entry stays. (No more O(n) scan
over all known handles to decide whether the bridge is still needed.)

### 1.3 `ServiceProxyContext`

The scope-side API surface a resolved proxy uses against its home
scope. Built once per resolve by `FxtrScope.resolve_service` and
passed to the registered `ServiceResolver` — proxies should not
reach into `FxtrScope` private members.

```python
@dataclass(frozen=True)
class ServiceProxyContext:
    home_scope: FxtrScope
    proxy_handle: IRHandle[Any]

    def mint_lease_for_handle(self, handle: IRHandle[Any]) \
        -> Lease[Any] | UnwrapForPipelining[Lease[Any], Any]: ...

    def fork_arena_handle_for_ids(self, ids: Collection[FxtrID]) \
        -> tuple[IRHandle[Any], Lease[Any]]: ...
```

- `mint_lease_for_handle` produces a (possibly pipelined) lease for
  any handle the home scope knows about. Pass `proxy_handle` for the
  proxy's own outer-dispatch lease, or any other handle to mint a
  lease to ship in a packet's `leases` list. Pipelined leases ride
  through `IRCallArg` cleanly — the dispatch path's
  `_unwrap_request_futures` resolves them before the receiver sees
  the packet, so callers don't need to distinguish.
- `fork_arena_handle_for_ids` forks the home scope's main arena to
  cover the given IDs (auto-lifting from ancestors as needed),
  wraps with `ArenaIRHandleAdapter`, and returns
  `(arena_handle, arena_lease)` ready to ship.
- Every method calls `home_scope.check_alive()` first; once drain
  begins all operations raise.

Bridge mechanics under the hood:

- Independent: clones the stored value via `_clone_independent`. The
  stored value may be a plain `Lease` (most common — clone the lease)
  *or* an `UnwrapForPipelining[Lease, ...]` (arises from a lift over
  a Dependent parent bridge whose converter returned pipelined; clone
  reuses the shared future and clones the inner keep-alive lease).
- Dependent: `lambda: bridge.converter(bridge.key_lease.clone(), home_scope._main_lease.clone())` — possibly pipelined.

`mint_lease_for_handle` and `resolve_service` both run through the
same lift path before producing a lease — so any handle reachable from
the home scope's parent chain ends up with a co-located entry on the
home scope after first contact, and we no longer rely on the parent
not having forgotten the handle in the meantime.

### 1.4 `ServiceRef` and `FxtrServiceImpl`

```python
@dataclass(frozen=True)
class ServiceRef[T]:
    handle: IRHandle[Any]
    target_type: type[T]

    async def resolve(self, scope: FxtrScope | None = None) -> T: ...

class FxtrServiceImpl[T](abc.ABC):
    @abstractmethod
    def _mount_as_fxtr_service(self, scope: FxtrScope) -> ServiceRef[T]: ...
```

`ServiceRef` carries no lease. The lease comes from a bridge looked up
at resolve / call time. Equality is value-based on `(handle,
target_type)`.

`FxtrServiceImpl` is the bare ABC — it does not define
`_service_exposed_methods` or any method-discovery convention. Those
live in `UntypedServiceImpl` (because they're specific to how
`UntypedService` reflects methods over the wire).

### 1.5 Resolution and "lift"

`ref.resolve(scope=None)` defaults to `scope = FxtrScope.get_active()`.
The lift logic (`FxtrScope._lift_bridge_for_handle`) is shared by
`resolve_service` and `ServiceProxyContext.mint_lease_for_handle` —
both produce a co-located entry on the home scope after first
contact:

1. **If `ref.handle in scope._known_handles`**: already locally
   registered; return the existing entry. Fast path; no walk.
2. Otherwise walk `scope._parents → ...` looking for `handle in
   parent._known_handles` (handles, *not* tokens — preserves the
   §1.2 "magical-prevention" rule). Error / return None if not found.
3. **If `ref.handle.token in scope._cap_bridges` (via a different
   handle that's already registered locally, OR via the
   `is_ancestor=True` entry for an ancestor's main token)**: share
   the existing local entry. Add `ref.handle` to its `handles` set
   and to `scope._known_handles`; do not install a duplicate
   bridge. This preserves the case-2 cycle invariant from
   `lifetime_conversion_mechanics.md` — the local bridge may be
   Dependent (e.g. our own `mount`-time identity converter), and
   storing a fresh `Lease[T]` on top of that would pin our own
   token via vias. The ancestor-entry case is the typical hit when
   a service is mounted on a parent's main token: the ancestor
   entry's `_IndependentBridge(lease=Lease[parent.main_token])`
   *is* exactly the right service-bridge shape for that handle.
4. Otherwise **lift**: mint a lease via the parent's bridge using
   `self._mint_lease_via_ancestor_bridge(parent_scope._token,
   parent_entry.bridge)` — sources `base` from `self`'s own
   ancestor entry for `parent_scope._token` (or
   `self._main_lease.clone()` when the bridge is on `self`
   itself), so the call works even when `parent_scope` is
   draining. Wrap the result in a fresh `_IndependentBridge`,
   install on `scope` as a new `_CapBridgeEntry`
   (`is_ancestor=False`), and add `ref.handle`. Subsequent resolves
   of any handle whose `_known_handles` slot lives in scope's
   chain short-circuit to the local entry.
5. Resolver step (resolve only): look up `ref.target_type` in the
   resolver registry. Invoke the matched resolver with a fresh
   `ServiceProxyContext(home_scope=scope, proxy_handle=ref.handle)`
   and return its result.

Lift-to-Independent (step 4) is safe because any token reachable via
the parent chain is structurally outlived by us — every direct
parent main-token has a matching `is_ancestor=True` entry on us,
populated at construction (and transitively for grandparents+); the
entries' leases live for our entire token lifetime. The via chain
inside the minted lease references things our own ancestor entries
already pin.

### 1.6 Why input ServiceRef args mount as Dependent (case 3)

A subtle but load-bearing point. When the user method body has a
`ServiceRef` argument, it's tempting to mount it on the call scope
as `_IndependentBridge` — we have a `Lease[arg_token]` from the
input set, just store it. Don't.

Why: the user method might *return* the same `ServiceRef` (or pass
it on to a sub-call that does). When packaging the return, the call
scope's bridge categorizes the token. If the bridge is Independent,
we'd emit `converter=None` and a clone of the stored lease. The
caller would then install another `_IndependentBridge` storing
*another* `Lease[arg_token]`.

Now the cycle: if the caller's *own* bridge for `arg_token` was
Dependent (the typical case — they `mount`-ed it on their own scope,
so handle.token is their own main_token), then the Lease[arg_token]
the caller now holds carries a via on caller's main_token. That via
pins caller's token alive until the stored lease is released. The
stored lease is released in the caller scope's `_on_token_end`
callback — which only fires once caller's token ends. Deadlock.

The fix is to mount ServiceRef args as `_DependentBridge` using
case 3 from `lifetime_conversion_mechanics.md`: `arg_token` is one
of `call_scope`'s derived inputs, so `call_scope` is derived *from*
`arg_token`. The converter wraps `derived.exchange_reverse(base)`
keyed by `STATIC_LIFETIME.lease()`. Each lookup mints a fresh
`Lease[arg_token]` from `Lease[call_scope_main]` (which structurally
carries `arg_token` as a via). At rest only the static key is
stored — no `Lease[arg_token]` pinning anything.

On return, the bridge categorizes as Dependent → we emit a chain
converter ret_ref → caller dedups against their existing bridge.
No cycle.

### 1.7 Service-resolver registry

```python
class ServiceResolver[T](Protocol):
    async def __call__(
        self,
        context: ServiceProxyContext,
        target_type: ServiceTypeAnnotation[T],
    ) -> T: ...

_SERVICE_RESOLVER_REGISTRY: TypeAnnotationHandlerRegistry[object, ServiceResolver[Any]] = ...

def register_service_resolver[T](
    for_type: RegistryKey[T],
    resolver: ServiceResolver[T],
) -> None: ...
```

Protocol-with-`__call__` over a bare `Callable` so the args are named
and have a single docstring home. Resolvers receive a fully-populated
`ServiceProxyContext` (carrying `home_scope`, `proxy_handle`, and the
scope-side helpers) — they don't poke at `FxtrScope` internals.
Resolvers can be plain `async def` functions (structural conformance)
or class instances.

Registry lives in `entity_ref_scope.py` next to
`register_entity_load_handler`; it starts empty.
`UntypedService`'s resolver self-registers from
`fxtr2/conversion/converters/untyped_service.py` at module import.

Generic / union / `NewType` / `TypeAliasType` unwrapping all come for
free from `TypeAnnotationHandlerRegistry`.

---

## 2. `UntypedService` design

### 2.1 Vocabulary

```python
type UntypedService = Any  # phantom-typed for protocol shape

type FxtrServiceData = (
    None | bool | int | float | str | bytes
    | FxtrID | EntityRef[Any] | ServiceRef[Any]
    | Mapping[str, "FxtrServiceData"]
    | Sequence["FxtrServiceData"]
)
```

Anything outside `FxtrServiceData` raises on conversion. Errors flow as
`IRException` per the standard Cap-IR path; `RejectMessage` carries
them across the wire.

### 2.2 Wire shapes (recap from overview)

**Input** = `IRTagged("fxtr_call", {method, args, kwargs, leases, fxtr_arena})`
where `args` / `kwargs` are lowered `FxtrServiceData` trees with
`EntityRef → IRTagged("fxtr_id", bytes)` and
`ServiceRef → bare IRHandle` (no lease). `leases` is the de-dup'd flat
list covering every distinct subject token referenced (handles in
args/kwargs + the arena's token if present). Receiver rebuilds
`{lease.subject: lease}` from it.

**Return** = `IRTagged("fxtr_return", {result, fxtr_arena, returned_refs})`
where `returned_refs` is one entry per distinct subject token in the
return:
- `converter is None`: token is "independent" from the callee's
  perspective. `key_lease` is a real `Lease[token]`. Caller installs
  with `attach_independent_leased_lowlevel_cap`.
- `converter is not None`: token is "dependent" on the callee's
  scope. `key_lease` is on a key brand the callee owns;
  `converter` is an `IRHandle[K]` whose dispatch produces
  `Lease[token]` from `(key_lease.clone(), <map of caller-side
  bases>)`. Caller wraps via `pipelined_lifetime_converter_from_handle`
  (suitably massaged for the multi-input form) and installs with
  `attach_dependent_keyed_lowlevel_cap`.

### 2.3 The proxy: `UntypedServiceProxy(scope, handle, lease_provider)`

`__getattr__(method_name) -> Callable[..., Awaitable[FxtrServiceData]]`.
Each call:

1. Walk `args` and `kwargs` separately, recursively, by isinstance:
   - Primitive: pass through.
   - `EntityRef`: emit `IRTagged("fxtr_id", bytes(ref.entity_id))`,
     remember the ID.
   - `ServiceRef`: emit `ref.handle` directly. Look up the home
     scope's `_cap_bridges` for `ref.handle.token`; remember its
     lease provider.
   - Sequence / Mapping: recurse.
2. If any FxtrIDs were seen: fork a sub-arena from `scope._main_arena`
   containing exactly those IDs; wrap with `ArenaIRHandleAdapter`;
   that's `fxtr_arena`. Lease for it: `scope._main_lease.clone()` —
   the arena adapter's token is the scope's token, so its lease is
   an Own bridge / scope-main lease.
3. Build the dedup'd `leases` list: walk every handle token referenced
   (arg/kwarg ServiceRefs + the arena's token if present), look up
   each via the home scope's bridge graph, mint one fresh lease per
   distinct subject token.
4. Build the `IRTagged("fxtr_call", …)` packet.
5. `await handle.request(lease_provider(), Call(method_name,
   packet))`. If `lease_provider()` returns a pipelined form,
   `handle.request` accepts it (substage 4).
6. Receive `fxtr_return`. On `IRException` re-raise. Otherwise:
   - Walk `result` collecting referenced handle tokens.
   - For each `returned_refs` entry: look up the token in the home
     scope. If already bridged: skip (no-op). Else install via
     `attach_independent_leased_lowlevel_cap` (converter-less) or
     `attach_dependent_keyed_lowlevel_cap` (converter present).
   - For any FxtrIDs in `result` + the `fxtr_arena`: bridge the arena
     into the home scope via `lazily_attach_from_arena`, using the
     same converter-vs-no-converter logic as for ServiceRefs.
     (`CapIRArena` adapts the IR-converter into a sync
     `LifetimeConverter` via the existing
     `pipelined_lifetime_converter_from_handle` machinery.)
   - Lift `result` back to a `FxtrServiceData` tree where handles
     become `ServiceRef[UntypedService]` and FxtrIDs stay as
     `FxtrID` / `EntityRef[Any]` (we just keep them as-is — the
     untyped path doesn't load entities).
7. Return the lifted result.

### 2.4 The impl: `UntypedServiceImpl(FxtrServiceImpl[UntypedService])`

- `@service_method` decorator (lives in `untyped_service.py`) tags a
  method as exposed; sets a sentinel attribute on the function.
- Default `_service_exposed_methods` walks the MRO collecting
  tagged methods; subclasses can override.
- `_mount_as_fxtr_service(scope)`:
  - Build a concrete `IRHandleLocalImplBase` subclass whose token is
    `scope.main_token` and task group is `scope._task_group`.
  - Its `_run_request_async` is the dispatch entry point — see §2.5.
  - Call `scope.mount_lowlevel_cap_as_service(handle,
    UntypedService)`. Return the resulting `ServiceRef`.

### 2.5 The dispatch task structure (per A in the discussion)

Per call, `_run_request_async(lease, Call("...", packet))` does:

```
async def _run_request_async(self, lease, request):
    send, recv = anyio.create_memory_object_stream(1)
    await scope._task_group.start(_dispatch_body, ..., send, ...)
    return await recv.receive()
```

`_dispatch_body(*, packet, send_stream, task_status)` runs in the
mount-scope's task group (so it survives the dispatch's return) and:

```
async def _dispatch_body(*, packet, send_stream, task_status):
    async with FxtrScope._with_derived_lifetime(
        derived_leases=[unique input leases from packet],
        mountable=True,
        include=[mount_scope],  # parent for memory store + bridge walk
    ) as call_scope, call_scope.as_active_scope():
        try:
            # Setup phase — anything that fails here surfaces via task_status.started()
            # 1. attach fxtr_arena (case-3 attach: arena.token in input set,
            #    call_scope.main_token derived from it -> use exchange_reverse-style converter)
            # 2. For each ServiceRef arg, attach as a Dependent bridge using
            #    case-3 (exchange_reverse) on the call scope's _main_lease:
            #      call_scope.attach_dependent_keyed_lowlevel_cap(
            #          handle=arg_ref.handle,
            #          service_type=UntypedService,
            #          key_lease=STATIC_LIFETIME.lease(),
            #          converter=<closure that exchange_reverses base, returns Lease[arg_token]>,
            #      )
            #    NOT attach_independent_leased_lowlevel_cap — see §1.6.
            # 3. Lift args/kwargs to user-facing FxtrServiceData. FxtrIDs stay
            #    as bare FxtrID (untyped service has no target type to bind);
            #    ServiceRefs stay as ServiceRef (already constructed during
            #    the walk).
            method = self._impl._find_exposed_method(packet.method)
            task_status.started()
        except BaseException:
            await send_stream.aclose()
            raise  # surfaces via task_group.start() raising on caller side

        try:
            user_result = await method(*args, **kwargs)
        except Exception as exc:
            ir_exc = (exc if isinstance(exc, IRException)
                      else IRException(message=str(exc), category=type(exc).__name__))
            await send_stream.send(("err", ir_exc))
            await send_stream.aclose()
            # No need to wait on drain — no ret_refs went out, so nothing
            # external is keeping the call_scope alive. async-with-exit
            # discards main + key, drain is prompt.
            return

        # Build return packet
        return_packet = build_fxtr_return(user_result, call_scope, ...)

        await send_stream.send(("ok", return_packet))
        await send_stream.aclose()

        # Falling out of `async with` triggers aclose.
        # If ret_refs have external holders, the derived output token
        # stays alive until they drop, and aclose blocks. Task group
        # stays alive throughout, so converter / arena dispatches keep
        # working. When all external clones drop, drain completes,
        # task group closes.
```

Outer `_run_request_async` plumbs the result:

- Receive the `("ok", packet)` or `("err", IRException)` tuple.
- On `"err"`, raise the IRException — the runner converts to a
  `RejectMessage`.
- On `"ok"`, return the packet — the runner emits a
  `ResolveMessage`.
- The original outer `lease` (the dispatch's lease on
  `mount_scope.main_token`) is discarded after the return is set
  (standard `IRHandleLocalImplBase` behavior).

### 2.6 Building the return packet

For each unique handle token `T` appearing in `user_result`:

- Find `T` in `call_scope._cap_bridges` (walking parents as
  needed).
- If found as `_IndependentBridge`: emit
  `ret_ref(T, key_lease=bridge.lease.clone(), converter=None)`.
- If found as `_DependentBridge`: build a chain converter handle
  (next paragraph) and emit
  `ret_ref(T, key_lease=Lease[K_wire], converter=<chain handle>)`.
- If not found: error — the user method returned a ref reachable
  from somewhere we can't see; we surface this as `IRException` with
  a clear category.

**Chain converter shape.** For a Dependent T:
- Wire token `K_wire` = `call_scope._key_ull.token` (the call
  scope's private key).
- The wire handle's `Call("convert", arg)` accepts:
  - Outer lease: `Lease[K_wire]`.
  - `arg`: a map of `{x_token_id: Lease[x_token]}` — the caller's
    bases for the call scope's `derived` inputs (everything except
    `K_wire`).
- Implementation: take the outer key lease and the arg leases,
  call `call_scope._derived.exchange_forward({K_wire: key, ...args})`
  to produce `Lease[call_scope.main_token]`, then call
  `bridge.converter(bridge.key_lease.clone(), call_scope_main_lease)`
  to produce `Lease[T]`.
- For the trivial case where `T == call_scope.main_token` (handle
  mounted via `mount_lowlevel_cap_as_service`), the bridge's
  converter is the identity — so the chain reduces to just
  `exchange_forward`.
- For deeper Dependent chains (`T` reached via a Dependent bridge in
  the call scope that itself came from a sub-call), the
  `bridge.converter` is itself an IRHandle dispatch — and the chain
  composes naturally with one extra Cap-IR hop per level.

The wire converter handle is mounted on `call_scope`'s task group.
Its lifetime is gated by `call_scope._key_ull` (so the caller can
keep dispatching it after `call_scope.main_token` ends, until the
caller drops their key clone).

For each unique FxtrID-bearing token in the return: same structure,
but the "ref" is the fxtr_arena handle — and the install on the
caller side uses `lazily_attach_from_arena` instead of a cap-bridge
attach.

---

## 3. File layout

| File | Adds |
|---|---|
| `fxtr2/data/entity_ref_scope.py` | `ServiceRef`, `FxtrServiceImpl` ABC, `_IndependentBridge` / `_DependentBridge`, `_cap_bridges` storage on `FxtrScope`, `mount_lowlevel_cap_as_service`, `attach_independent_leased_lowlevel_cap`, `attach_dependent_keyed_lowlevel_cap`, `mount`, `_get_lease_for_token`, `_resolve_service`, the empty `_SERVICE_RESOLVER_REGISTRY` + `register_service_resolver` |
| `fxtr2/conversion/converters/untyped_service.py` (new) | `UntypedService` type alias, `FxtrServiceData`, `@service_method`, `UntypedServiceImpl`, `UntypedServiceProxy`, dispatch-packet build/parse helpers, `_dispatch_body`, `UntypedService` resolver registration |
| `tests/test_untyped_service.py` (new, top-level convention) | Acceptance + unit tests |

The decision (per discussion §3) is to keep the bridge primitives
co-located with `FxtrScope` because of the circular-reference
challenges that historically forced `EntityRef` and `FxtrScope` to
share a file. `@service_method` and the `UntypedService` machinery
live in `conversion/converters/` because that's the natural home
for "how do we marshal Python objects to/from the wire" — same
shelf as `mashumaro_entities.py`.

---

## 4. Implementation order

We're pair-programming, so this is a guide, not a contract. Each
step ends with something runnable.

1. **Bridge primitives + scope wiring.** ✅ *Landed.*
   `_IndependentBridge`, `_DependentBridge`, `_known_handles` +
   `_cap_bridges`, the five scope methods
   (`mount_lowlevel_cap_as_service`,
   `attach_independent_leased_lowlevel_cap`,
   `attach_dependent_keyed_lowlevel_cap`, `mount`, `forget_service`),
   `_find_bridge_for_handle` parent-walk, public `resolve_service`,
   scope-token-end teardown. `ServiceRef` + `FxtrServiceImpl` ABC +
   `ServiceResolver` Protocol + empty `_SERVICE_RESOLVER_REGISTRY` +
   `register_service_resolver`. Tests in
   `tests/test_entity_ref_scope/test_cap_bridges.py`.
   `attach_*_lowlevel_cap` methods take ownership of supplied leases
   up-front so error-path raises don't leak. Handle-tracking is
   per-handle (not per-token) so resolving an unrelated handle on the
   same token through a scope that only has handle A mounted does not
   succeed by accident.

   **1b. Bridge-storage reorganization.** ✅ *Landed.* Setup work for
   step 5 (return-flow). Three changes:
   - `_IndependentBridge.lease` is now
     `Lease[Any] | UnwrapForPipelining[Lease[Any], Any]` so a lift over
     a Dependent parent bridge whose converter returns pipelined can
     stash the result as-is instead of awaiting eagerly. Helpers
     `_clone_independent` / `_discard_independent` cover both shapes.
   - Each `_cap_bridges` value is now a `_CapBridgeEntry(bridge,
     handles: set)`; the per-entry `handles` set replaces the O(n)
     `forget_service` scan over `_known_handles`.
   - `_lift_bridge_for_handle` is invoked by both `resolve_service`
     and `ServiceProxyContext.mint_lease_for_handle`, giving the
     stronger invariant that every reachable handle ends up with a
     local entry on first contact (no more "walk every time + trust
     the parent didn't forget"). The §1.2 "magical-prevention" rule is
     preserved: a stranger handle on a known token still fails to
     resolve because we walk parents to verify reachability before any
     local short-circuit. New tests in
     `tests/test_entity_ref_scope/test_cap_bridges.py`:
     `TestLiftOnResolve` (4) and `TestIndependentBridgeStorageHelpers`
     (3); existing tests updated to access `.bridge` / `.handles` on
     entries.
2. **`UntypedService` minimal end-to-end.** ✅ *Landed.*
   New module `fxtr2/conversion/converters/untyped_service.py`:
   `UntypedService`, `FxtrServiceData`, `@service_method`,
   `UntypedServiceImpl`, private `_UntypedServiceHandle`
   (`IRHandleLocalImplBase`), `UntypedServiceProxy`, and the
   `_dispatch_body` task with the `anyio` memory-stream handoff per
   §2.5. Wire shapes use `IRTagged("fxtr_call", …)` / `IRTagged("fxtr_return", …)`
   with `leases` / `fxtr_arena` / `returned_refs` slots reserved for
   later steps. Tests in `tests/test_conversion/test_untyped_service.py`
   (24 tests). Notes / minor deviations:
   - Args/kwargs validation happens both on the proxy (raises
     `TypeError` locally) and on the receiver (returns
     `IRException(category="MalformedPacket")`). Belt-and-braces; the
     proxy-side check catches typos before any wire activity.
   - Dunder names (`__name__`, `__nonexistent__`) raise
     `AttributeError` on the proxy rather than going through the wire.
     Underscore-prefixed but non-dunder names *do* go through.
   - Tuples normalize to lists across the wire (the IR's `Sequence`
     form). Tests expect `list` shape on round-trip.
   - Per-call child scope is created via `_with_derived_lifetime` with
     empty `derived_leases` (since step 2 has no input leases). The
     scope is mountable so user methods can `mount`/`attach` (not
     exercised yet — step 4 picks that up).
   - The body uses `task_status.started()` to fence the setup phase;
     pre-`started` errors propagate via `task_group.start`'s raise,
     post-`started` errors flow over the stream as `("err", IRException)`.
3. **FxtrIDs in args/results.** ✅ *Landed (args-side + pass-through
   returns; new-ID returns deferred to step 5).*
   - `FxtrServiceData` extended with `FxtrID` and `EntityRef[Any]`.
     `_encode_fxtr_service_data` / `_decode_fxtr_service_data`
     mirror the wire encoding from `cap_ir_arena.py`'s
     `_encode_fxtr_id` (tagged-bytes form, provenance parents
     stripped). Decoding always produces a bare `FxtrID` — the
     untyped path drops `EntityRef`'s target-type annotation.
   - Proxy: walks args/kwargs collecting referenced `FxtrID`s. If
     any, forks a sub-arena off the home scope's main arena via
     `MemoryArena.copy_to_new_arena`, wraps with
     `ArenaIRHandleAdapter` (uses `home_scope._task_group`), and
     ships in `packet["fxtr_arena"]` plus a single
     `Lease[home_scope.token]` in `packet["leases"]`. Calls with
     `FxtrID` args therefore require a *mountable* home scope.
   - Receiver: `_take_input_leases` (new helper) drains
     `packet["leases"]` into a `{lease.subject: lease}` map for
     `_with_derived_lifetime(derived_leases=...)`. The call scope's
     derived inputs now include the home-scope token. The arena
     handle is wrapped with `CapIRArena.direct` and bridged via
     `lazily_attach_from_arena` for each referenced ID, with a
     case-3 converter (`derived.exchange_reverse(base)` returning
     `Lease[home_scope_token]`, `STATIC_LIFETIME` key). The dispatch
     error path is unified: `_run_user_method` raises `IRException`
     for any structural / value error and `_dispatch_body` catches
     once.
   - Step-3 returns: only **pass-through** `FxtrID`s — IDs that came
     in via the input arena and are therefore already tracked in
     the home scope. Returning a new ID raises
     `IRException(category="MalformedReturn", "not in the input
     set")`. Full ret_ref chain-converter lands in step 5.
   - Tests in `tests/test_conversion/test_untyped_service.py`
     (89 runs total via parametrisation). Most tests use a
     `service_layout` fixture that runs each test against three
     mount/caller configurations: `same_scope`, `child_scope`, and
     `sibling_attach` (caller explicitly registers the handle via
     `attach_independent_leased_lowlevel_cap`, simulating what the
     RPC bridge will do in substage 7). The same parametrisation is
     the canonical "operations on mounted services work as expected
     regardless of layout" check; future RPC variant slots in as a
     fourth `params` value.
   - **Pre-existing-bug fix:** `bare_lazily_attach_from_arena` now
     closes the freshly-forked source arena when
     `_adopt_inheritance_arena_claim` rejects the new claim because
     X_reg is dominated by an existing antichain entry. Without the
     fix, the abandoned fork's internal resources (notably
     `CapIRArena._remote_lease`) leaked. Surfaced when the
     `same_scope` layout exercises the dominated branch — the lease
     fixture caught it.
4. **ServiceRefs in args.** ✅ *Landed.*
   - `FxtrServiceData` extended with `ServiceRef[Any]`. Wire encoding:
     `IRTagged("service_ref", IRHandle)`. Decoding always produces
     `ServiceRef(handle, target_type=UntypedService)` — the untyped path
     drops any specific target-type annotation, mirroring how
     `EntityRef` decoded to bare `FxtrID`.
   - Encoder / decoder helpers gained a `service_handles` out-parameter
     set so the proxy and receiver can both enumerate the bare handles
     decoded from the packet.
   - **Proxy**: walks args/kwargs collecting `arg_service_handles`. For
     each unique handle, walks the home scope's `_cap_bridges` (via
     `_find_bridge_for_handle`) and mints a concrete lease via
     `_mint_lease_for_bridge`. The lease goes into `packet["leases"]`
     deduped by subject token. Pipelined leases from the home-scope
     bridge raise `NotImplementedError` for now (concrete-only inside
     packets).
   - **Receiver**: input leases → derived call-scope (already done in
     step 3); decoded args' `arg_service_handles` are bridged into the
     call scope as `_DependentBridge`s via
     `attach_dependent_keyed_lowlevel_cap` with a case-3 converter
     (`_make_case3_converter` — extracted helper shared with the
     `fxtr_arena` bridge). Mounting as Dependent (not Independent) is
     load-bearing: it avoids the return-path cycle described in §1.6.
   - **Returns**: like FxtrIDs, returned ServiceRef handles are
     restricted to the pass-through set (handles that came in via
     input args). Returning a new ServiceRef raises
     `MalformedReturn` until the chain-converter ret_ref machinery
     lands in step 5.
   - Tests in `tests/test_conversion/test_untyped_service.py` —
     `TestServiceRefArg` (4 tests × 3 layouts = 12 runs). The tests use
     a small `_make_ref_visible_from_caller` helper to ensure the
     "peer" ref (the second mounted impl) is visible from
     `caller_scope` regardless of layout (idempotent for
     `same_scope`/`child_scope`; required for `sibling_attach`).
5. **ServiceRefs in returns — chain converter.** ✅ *Landed.*
   - **Bridge generalization.** `_DependentBridge.leases:
     dict[LifetimeToken, StoredLeaseValue]` (widened union allows
     `Lease | UnwrapForPipelining`). Existing simple converters
     migrated via `_make_single_key_converter` adapter; user-facing
     `attach_dependent_keyed_lowlevel_cap` API stays single-key.
     Token-end teardown + mint clone uniformly via
     `_clone_independent` / `_discard_independent`.
   - **CapIRArena multi-input.** `with_converter` factory takes
     `x_tokens: Sequence[LifetimeToken]`; `_make_y_lease` extracts
     each in declared order and dispatches `Call("convert",
     [x_leases…])`. Empty `x_tokens` allowed in with-converter mode
     (the no-input chain-converter case). Test helper
     `_ExchangeForwardConverter` migrated to the sequence shape;
     existing acceptance tests updated.
   - **Wire shape.** `fxtr_return` packet's `returned_refs` is
     replaced with `new_indep_leases: list[Lease]` and
     `new_dep_leases: list[{token, key_lease, converter,
     input_tokens}]`. Tokens already in
     `dispatch_scope._derived.inputs` are skipped (caller already
     has access). The shape lives in untyped_service.py constants
     `_DEP_REF_TOKEN` / `_DEP_REF_KEY_LEASE` / `_DEP_REF_CONVERTER`
     / `_DEP_REF_INPUT_TOKENS`.
   - **Callee chain handle.** `_ChainConverterHandle` is an
     `IRHandleLocalImplBase` keyed on `K_call`, mounted on
     `call_scope._task_group`. Snapshots the dispatch-scope bridge
     for `target_token` at construction (clones every entry of
     `bridge.leases`, captures `bridge.converter`); on dispatch,
     `derived.exchange_forward` consumes the input leases →
     `Lease[call_scope.main]`, then `bridge.converter` runs to
     produce `Lease[target_token]`. The "target == call_scope.main
     and no service mounted" case (typical for new-FxtrIDs-only
     returns) synthesises an identity bridge so `exchange_forward`
     suffices.
   - **Caller chain bridge install.** `_bridge_in_dep_ret_ref`
     builds a `_DependentBridge` whose `leases` dict merges
     snapshots of home-scope's bridges for each non-base input
     token (`_IndependentMintSpec` / `_DependentMintSpec` /
     `_UseBaseSpec`); the merged dict + a closure over the wire
     handle + input-token order make up the mint logic. Robust
     against `forget_service` on the home-scope bridges (snapshots
     are taken at install time).
   - **Caller arena attach.** `_install_returned_arena` follows
     the substage-5 pattern: a local `DerivedLocalLifetime` `D`
     whose inputs are `{K_call} ∪ ({home_main} if applicable)`;
     a sync local converter does `D.exchange_forward(...)` →
     `Lease[D]`; `CapIRArena.with_converter(z=D, x_tokens=…,
     k_token=K_call, remote=arena_handle, converter=wire_handle)`
     handles the `D → arena_token` step internally (pipelined
     dispatch lives inside `CapIRArena._make_y_lease`); the store
     sees only concrete `Lease[D]` values.
   - **`_with_derived_lifetime` API change.** Added required-by-
     position `include_leases: Sequence[Lease]` parameter; the
     factory takes ownership of these leases and registers them
     directly with the memory store as parents. The old
     "clone parent's `_main_lease` for me" helper is gone (only
     `__init__` still does that, inlined). The draining-parent
     check moved from `_init_common` to `__init__`, so
     `_with_derived_lifetime` allows a draining parent provided
     the caller supplies a lease that keeps the parent reachable.
     `_dispatch_body` takes advantage of this: it passes the
     dispatch's outer lease as `include_leases`, which is what
     makes the spawner pattern's sub-dispatch-into-draining-parent
     case work.
   - **Self-mount restriction (NotImplementedError).**
     ~~`resolve_service` rejects `ServiceRef`s whose
     `handle.token is self._token` (the canonical case is
     resolving a service into the same scope it was mounted into).~~
     **Lifted in `part6post3_derived_scope_fixes.md`** — the
     dispatch path now routes the outer lease through
     `derived_from_leases` so the call scope's main token is
     derived from the parent's, breaking the chain-bridge drain
     cycle structurally. The `parent_loads_from_child` cycle was
     already gone after `part6post2`; both `same_scope` failures
     are now real passes.
   - **Status.**
     - **Landed:** bridge generalization (dict form,
       `StoredLeaseValue` union); `CapIRArena.with_converter`
       multi-input + empty-`x_tokens`; new wire shape
       (`new_indep_leases` / `new_dep_leases` with
       `input_tokens`); `_ChainConverterHandle` callee-side;
       `_bridge_in_dep_ret_ref` caller-side install with
       snapshot-based `_InputMintSpec` variants;
       `_install_returned_arena` (single-input case) using the
       part5 local-derived-`D` pattern;
       `_extract_arena_install_params` cleanup of the
       arena-resource extraction; `_with_derived_lifetime`
       `include_leases` parameter (later renamed to
       `independent_leases` in `part6post3`); self-mount
       restriction in `resolve_service` (later removed in
       `part6post3` — see below). All previously-passing tests
       stay green; same_scope parametrizations of the
       `service_layout` fixture xfailed centrally with
       `raises=NotImplementedError, strict=True` until
       `part6post3` flipped them to passing.
     - **Tests:** `TestSpawnerPattern.test_spawn_and_invoke_echo`
       (spawner pattern), `test_spawn_and_store_in_one_call`
       (mixed service-ref + new-FxtrID return),
       `test_multi_input_arena_attach` (multi-input arena attach
       — now passing). `child_scope` and `sibling_attach`
       parametrizations pass; `same_scope` is xfail throughout.
     - **Step 5 follow-up (landed):** unified arena attach +
       service-handle install. Both paths now go through a
       single chain-bridge primitive: `_install_returned_resources`
       indexes the wire packet, builds an `_IndependentBridge` /
       `_DependentBridge` per unique referenced token, then
       installs it (handles via `attach_*_lowlevel_cap`; arena
       via `_install_arena_with_bridge` wrapping the bridge in
       `CapIRArena.with_converter`). Multi-input arena attach
       falls out for free because `_build_chain_bridge` already
       handles arbitrary input snapshotting; the previous
       `UnsupportedReturn` stub is gone. As a side effect:
         - `CapIRArena.with_converter` now takes a Python
           `DependentBridgeConverter` (+ `bridge_leases` /
           `base_token`) instead of an `IRHandle[K]` converter
           with `x_tokens` / `k_token`. The arena owns its
           at-rest bridge leases and discards them on `close()`.
           Tests that previously built `CapIRArena.with_converter`
           with an IR-handle converter use the new
           `_make_irhandle_bridge` helper in `test_cap_ir_arena.py`
           to wrap the handle.
         - `StoredLeaseValue` / `DependentBridgeConverter` /
           `clone_stored_lease` / `discard_stored_lease` moved
           from `entity_ref_scope.py` to
           `leases/leased_cap_common.py` so `cap_ir_arena.py`
           can import them without a circular dependency. The
           old `_clone_independent` / `_discard_independent`
           private helpers were renamed to public names.
         - `_install_arena_with_bridge` closes the original
           `cap_ir` after `lazily_attach_from_arena` (the store
           retains the sibling produced by
           `cap_ir.copy_to_new_arena`; the original's bridge
           state would otherwise leak its at-rest leases).
         - `_extract_arena_install_params` and
           `_bridge_in_dep_ret_ref` are gone.
     - **Step 5 follow-up (landed): scope-side ancestor lease
       tracking.** The lift path no longer reaches into
       `bridge_scope._main_lease` — which is gone for draining
       parents. Instead each `FxtrScope` carries its own
       structural ancestor leases:
         - `_CapBridgeEntry` gained `is_ancestor: bool = False`.
           Service entries (mount/attach/lift) stay
           `is_ancestor=False`; **ancestor entries** are populated
           at scope construction (one `_IndependentBridge` per
           direct parent, plus transitively from the parent's own
           ancestor entries). They co-exist on the same
           `_cap_bridges` dict, keyed by token. `forget_service`
           preserves `is_ancestor=True` entries; `_on_token_end`
           still tears down both kinds.
         - `_populate_ancestor_bridges(parent_to_lease)` is
           called from both constructors. For now ancestor
           entries must be `_IndependentBridge` with concrete
           `Lease`; Dependent / pipelined ancestor cases raise
           `NotImplementedError` with a TODO — those will be
           needed when the memory store's parent tracking moves
           fully to the scope layer.
         - The static helper `_mint_lease_for_bridge` is gone.
           Replaced with the self-method
           `_mint_lease_via_ancestor_bridge(ancestor_main_token,
           bridge)` which sources the converter's `base` from
           `self`'s own ancestor entry for `ancestor_main_token`
           (or `self._main_lease.clone()` when the bridge is on
           `self` itself). Lifts now work cleanly through draining
           parents — the `TestMutuallyReferencingScopes` acceptance
           test exercises the end-to-end S1/B-in-S2/C-back-to-A
           round-trip without touching any draining-parent code
           paths in the lift.
     - **Acceptance suite (landed in step 5; step 7's
       responsibility per the original substage list):**
       `TestSpawnerPattern.test_spawn_and_invoke_echo`,
       `test_spawn_and_store_in_one_call`,
       `test_multi_input_arena_attach`,
       `test_chain_of_chain_returns` (multi-level
       `_DependentMintSpec` path on the service-handle side),
       `test_multi_input_arena_attach_with_dependent_peer`
       (same shape on the arena side), and
       `TestMutuallyReferencingScopes.test_dag_drains_cleanly`
       (the §5 mutually-referencing-scopes scenario). All pass
       on `child_scope` and `sibling_attach`; `same_scope` is
       xfail throughout pending the memory-store redesign.
     - **Punted to follow-ups:**
       - **Memory-store redesign** to lift the self-mount
         restriction *and* migrate parent tracking off the
         store onto the scope. Tracked in
         `parent_loads_from_child_inheritance_cycle.md` and
         `resolve_into_mount_scope_drain_cycle.md`. The
         `is_ancestor` machinery above is the prep step; once
         the store-side parent tracking moves over, scopes
         become the single source of truth for ancestor
         leases. The Dependent / pipelined ancestor
         `NotImplementedError`s in
         `_populate_ancestor_bridges` /
         `_mint_lease_via_ancestor_bridge` are the natural
         relaxation points.
       - **Async cancel propagation across chain returns.** Not
         exercised yet — was on the original step 7 acceptance
         list. Worth a dedicated test (e.g. cancelling a
         returned-ref dispatch mid-flight; verifying clean
         drain).
6. **Lift on resolve.** ✅ *Landed in 1b above.*
7. **Acceptance suite.** ✅ Mutually-referencing scopes (your
   S1/A/B/C scenario), deep chain returns, async cancel
   behavior, the lease-leak fixture stays green throughout.

After step 5 we have a working untyped service system; steps 6
and 7 are polish + verification.

---

## 5. Open design questions

Things we deliberately haven't pinned down — to be resolved during
implementation:

- **Wire shape for the multi-input chain-converter call arg.**
  `IRValue` mappings only allow `str` keys (per the JSON codec).
  So we send the input-token-leases map as a list of pairs:
  `[(LifetimeToken, Lease), ...]`. `LifetimeToken` flows through
  Cap-IR via bounce-back identity, so it can appear directly in
  `IRValue` (no encoding needed). Receiver rebuilds the map
  before calling `derived.exchange_forward(...)`.
- **Drain blocking on `aclose` for mountable derived scopes
  with external lease holders.** This already works in substage 3
  for the mount-scope side (the dispatch task lives in the
  parent's task group and the parent's `aclose` waits). For the
  call_scope itself, `_dispatch_body`'s `async with` will block
  on `aclose` waiting for ret_ref holders. Verify there's no
  deadlock between this and the parent's own drain (we always
  drain children first, so should be OK).
- **Lift safety with parent-bridge external dependencies.**
  Lift assumes the parent bridge's via chain only references
  things our scope already pins structurally. Could fail if the
  parent's bridge has a converter with an external key on a
  brand we don't control. Probably fine for substage 6 (no path
  yet to construct such bridges); revisit if step 6 surfaces a
  case.
- **`_dispatch_body` uses `task_group.start`** (not
  `start_soon`). Lets us surface setup-phase errors via the
  caller's `start()` raising, before the dispatch's outer task
  emits a future to the IR caller. The setup phase ends at
  `task_status.started()`, after the call scope is open and
  args are mounted.
- **Chain converter complexity.** For the spawner pattern (step
  5) we'll see how messy the chain ends up. May want to flatten
  multi-level chains at the callee side before sending — defer
  the call until we hit it.
- **Acceptance test 4 (mutually referencing).** ✅ *Landed* as
  `TestMutuallyReferencingScopes.test_dag_drains_cleanly`.
  Note on drain order: the test nests S2 (the service-host)
  outer and S1 (the bridge-holder) inner — same convention as
  `mounted_service`'s `sibling_attach` layout. Reversing the
  nesting deadlocks for the standard reason: S1's
  `attach_independent_leased_lowlevel_cap` to B is contractually
  a hold on `Lease[S2.main]` until S1 drains, so S1 must close
  before S2 can. (No implementation change needed; the test
  just has to follow the convention.)

---

## 6. Followups (out of scope for substage 6)

- **`unmount`.** Today a service lives until the scope drains.
  Adding unmount probably means generalizing
  `mount_lowlevel_cap_as_service` to take its own derived
  lifetime so unmounting can revoke just that one bridge — but
  it's tricky in the presence of in-flight dispatches and not
  needed yet.
- **Typed service support** is substage 8.
- **Performance — chain converter flattening.** The chain
  converter adds one Cap-IR hop per level of nesting; if real
  workloads find this expensive we can flatten at the callee
  side before sending.
- **Performance — bridge lease caching.** Currently every
  `lease_provider()` call on a Dependent bridge re-runs the
  converter (potentially a Cap-IR dispatch round-trip). An
  obvious optimization is to cache the minted lease (or
  `UnwrapForPipelining` form) on the bridge while the scope
  is active, and drop all cached leases at the *start* of
  drain (so cached vias on our own token don't pin us).
  Independent bridges trivially "cache" via their stored lease
  already; this would extend the same fast-path to Dependent.
  Need to handle the pipelined case carefully (await once at
  first use, store the resolved lease; or store the
  `UnwrapForPipelining` and re-walk). Worth doing if step 5's
  chain-converter cost shows up in profiling — flag during
  implementation. The dispatch boundary is a natural cache
  miss point: on return, the caller's scope doesn't have a
  cached lease for the new ref, but it can populate one on
  first use.
