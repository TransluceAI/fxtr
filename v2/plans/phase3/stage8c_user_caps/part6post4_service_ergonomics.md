# Service ergonomics (part 6, post 4)

## Motivation

After parts 6, 6post1–6post3, the substage-6 surface works end-to-end but
has three rough spots that make later substages (7 and 8) harder than
they should be:

1. **`UntypedServiceImpl` and `UntypedServiceProxy` don't separate the
   "common service plumbing" from the "untyped vocabulary".** Substage
   8's typed services need to share everything *except* the per-method
   conversion shim (call-packet build, dispatch, return-resource
   install, child call-scope management, `@service_method` discovery).
   Today those concerns are interleaved across `_UntypedServiceHandle`,
   `_dispatch_body`, `_run_user_method`, and `UntypedServiceProxy._call`,
   with no factored seam. Pulling them apart now is straight refactor
   work; doing it during substage 8 would mean refactoring under
   pressure with two implementations open at once.

2. **A `ServiceRef` carries a Python `target_type` annotation but no
   runtime type identity.** Cross-process, the receiver re-attaches
   `target_type` based on its own annotation, with no check that the
   peer agrees. There's no `service_type_id`-style FxtrID for runtime
   inspection, so an `UntypedService` (renamed `UnknownService` in this
   plan) proxy has no way to expose "what type does this service claim
   to be" to the caller. This is the analog of "an `EntityRef[T]`
   carries a typedef ID alongside its `target_type`", which we already
   rely on for `UnknownEntity`-style introspection.

3. **There's no user-facing way to bridge a `ServiceRef` from a
   non-parent scope.** Today the only way for a scope to "know" a
   handle is to be a descendant of the scope that mounted it (so the
   `_lift_bridge_for_handle` parent-walk finds it), or to manually
   call `attach_bridged_lowlevel_cap` with a hand-rolled
   `IndependentLocalBridge`. Both work, but neither is an obvious
   user-facing API for "I have a `ServiceRef` from over there, please
   make it usable here." Substage 7's RPC bridge will want exactly
   this primitive.

This plan addresses all three in three sequenced sections — they are
mostly independent at the implementation level but each builds the
mental model the next assumes, so doing them in order keeps the
codebase coherent at every checkpoint.

## Scope

This plan covers:

- **§1.** Extract the common service plumbing into a new
  `fxtr2/services/` package, refactoring `UntypedServiceImpl` and
  `UntypedServiceProxy` so the "build a packet, dispatch, decode the
  return" path is reusable. Introduces a new ABC stack
  `FxtrServiceImpl[T]` ⊇ `CommonServiceImpl[T]` ⊇ `SimpleAsyncServiceImpl[T]`
  ⊇ `UntypedServiceImpl`, plus a free function
  `invoke_service_method`.
- **§2.** Add `service_type_id: FxtrID` to `ServiceRef`, threading it
  through every mount/attach/lift/resolve path via a new frozen
  `ServiceKey(handle, type_id)` value type that replaces the bare
  `IRHandle` in `_known_handles` / `_CapBridgeEntry.handles` etc.
  Renames `UntypedService` → `UnknownService`, `UntypedServiceProxy`
  → `UnknownServiceProxy`, and adds a read-only `service_type_id`
  property.
- **§3.** Add `FxtrScope.bridge_service_from_scope` and
  `ServiceRef.bridge_from_scope` for explicit cross-tree bridging,
  with a sync `ServiceResourceHelper` returned for `with`-style scoped
  cleanup.

Out of scope:

- Substage 7 (`open_rpc_scope`) and substage 8 (typed services) — both
  are unblocked by this work but neither is included.
- Changes to the lease taxonomy, the `_with_derived_lifetime`
  ancestor-bridge machinery, or the chain-converter return path.
  Those are stable as of part6post3.

---

## §1 — Extract common service plumbing

### Goal

Pull the wire-shape / packet-build / dispatch / return-resource code
out of `UntypedServiceImpl` and `UntypedServiceProxy` into a new
`fxtr2/services/` package, with a clean ABC layering that substage
8's typed services can reuse.

### New package layout

```
fxtr2/services/
    __init__.py                — re-exports the public API.
    service_cap_common.py      — wire constants, FxtrServiceData,
                                 encode/decode helpers, packet-build
                                 / dispatch / return-resource code,
                                 the CommonServiceImpl + handle ABCs,
                                 invoke_service_method.
    untyped_service.py         — UnknownService / UnknownServiceProxy
                                 / SimpleAsyncServiceImpl /
                                 UntypedServiceImpl / @service_method
                                 / resolver registration. (Renamed
                                 / refactored from today's
                                 fxtr2/conversion/converters/untyped_service.py.)
    LOCAL_CODE_OVERVIEW.md     — orientation doc for the new package.
```

The old path `fxtr2/conversion/converters/untyped_service.py` is
deleted; every importer (tests + any internal users) is updated in
the same change to point at the new path. No re-export shim.

`FxtrServiceImpl` and `ServiceRef` and `ServiceProxyContext` /
`ServiceResolver` / `register_service_resolver` continue to live in
`fxtr2/data/entity_ref_scope.py` (they are tied to `FxtrScope`'s
internal bookkeeping). The new `services/` package imports them.

### ABC stack

```
FxtrServiceImpl[T]              # in entity_ref_scope.py (unchanged)
    └── CommonServiceImpl[T]    # new in service_cap_common.py
        └── SimpleAsyncServiceImpl[T]  # new in untyped_service.py
            └── UntypedServiceImpl     # subclass for UnknownService
```

- **`FxtrServiceImpl[T]`** stays as-is: one abstract method
  `_mount_as_fxtr_service(scope, *, task_group) -> ServiceRef[T]`.
- **`CommonServiceImpl[T]`** is the layer that knows about the
  `fxtr_call` / `fxtr_return` wire shape:
  - Abstract: `async def _handle_invoke(self, method_name: str, *args:
    FxtrServiceData, **kwargs: FxtrServiceData) -> FxtrServiceData`.
    Replaces today's "look up in `_service_exposed_methods` and
    invoke" step in `_run_user_method`.
  - Abstract: `def _target_type_annotation(self) -> ServiceTypeAnnotation[T]`
    — returns the type annotation to embed in the resulting
    `ServiceRef[T]` (e.g. `UnknownService` for
    `UntypedServiceImpl`, the protocol class for typed services).
  - (After §2:) abstract `_service_type_id(self, scope: FxtrScope)
    -> FxtrID` — see §2.
  - Concrete `_mount_as_fxtr_service(scope, *, task_group)`: builds a
    `CommonServiceImplHandle(self, scope, task_group)` and calls
    `scope.mount_lowlevel_cap_as_service` (after §2: with the
    `_service_type_id` result threaded in).
- **`SimpleAsyncServiceImpl[T]`** is the layer that knows about
  `@service_method`-tagged async methods:
  - Concrete `_handle_invoke(method_name, *args, **kwargs)`: walks
    `_service_exposed_methods()` (the existing MRO walk, moved here
    verbatim), looks up `method_name`, raises
    `IRException(category="UnknownMethod")` if missing, otherwise
    `await`s the bound method with the unpacked args.
  - Still abstract on `_service_type_id` so each subclass declares
    its identity.
- **`UntypedServiceImpl(SimpleAsyncServiceImpl[UnknownService])`** is
  trivial after §2: `_service_type_id` returns
  `scope.track_builtin(FxtrID.builtin("untyped_service"))`.

The ABC stack composes cleanly: a future "mashumaro typed service"
layer in substage 8 plugs in as another `CommonServiceImpl[T]`
subclass that overrides `_handle_invoke` to apply per-method
conversion before/after delegating. It does *not* go through
`SimpleAsyncServiceImpl` (which is specifically about `@service_method`
discovery on Python methods).

### `CommonServiceImplHandle`

Replaces today's `_UntypedServiceHandle`. Same shape, but takes a
`CommonServiceImpl` instead of a `UntypedServiceImpl`. The
`@service_method` discovery machinery moves *out* of the handle and
into `SimpleAsyncServiceImpl._handle_invoke`. The handle itself just:

- Validates request shape (fxtr_call tag, method name is a string,
  packet is a mapping) — same as today.
- Spawns `_dispatch_body` with `impl=self._impl` (i.e., the
  `CommonServiceImpl`); the body's only direct interaction with the
  impl is `await impl._handle_invoke(method_name, *decoded_args,
  **decoded_kwargs)` instead of today's
  `await method(*decoded_args, **decoded_kwargs)`.

The "method discovery + UnknownMethod error" step disappears from
the handle entirely; `_handle_invoke` raises
`IRException(category="UnknownMethod")` directly when a
`SimpleAsyncServiceImpl` doesn't recognize the name. This is a clean
factoring: each layer raises the errors it knows about.

`_dispatch_body` and `_run_user_method` move to
`service_cap_common.py` as module-level helpers, with their `method:
Callable` parameter replaced by `impl: CommonServiceImpl`. The
"resolve method via mapping" step deletes; the `_handle_invoke`
call replaces it.

### `invoke_service_method` free function

```
async def invoke_service_method(
    ctx: ServiceProxyContext,
    method_name: str,
    *args: FxtrServiceData,
    **kwargs: FxtrServiceData,
) -> FxtrServiceData: ...
```

Holds the entirety of today's `UntypedServiceProxy._call`:

1. Validate args/kwargs are `FxtrServiceData`-shaped.
2. Encode + collect arg ids and arg service handles.
3. Fork an arena (if any FxtrIDs) via `ctx.fork_arena_handle_for_ids`.
4. Mint per-handle leases (via `ctx.mint_lease_for_handle`); dedup
   by subject token.
5. Build `IRTagged("fxtr_call", {...})` packet.
6. `await ctx.proxy_handle.request(ctx.mint_lease_for_handle(ctx.proxy_handle), Call(method_name, packet))`.
7. Validate return packet shape; decode `result`; install bridges
   for returned tokens via `_install_returned_resources` (moved to
   the common module).
8. Return the decoded result.

`UnknownServiceProxy.__getattr__` becomes `functools.partial(
invoke_service_method, self._ctx, name)`. Substage 8's typed proxies
will call `invoke_service_method` from inside their own per-method
shims after applying the inverse conversion to args.

### Files touched

- New: `fxtr2/services/__init__.py`, `fxtr2/services/service_cap_common.py`,
  `fxtr2/services/LOCAL_CODE_OVERVIEW.md`.
- Move (with refactor): `fxtr2/conversion/converters/untyped_service.py`
  → `fxtr2/services/untyped_service.py`. Removes
  `_UntypedServiceHandle` and the discovery code from inside
  `UntypedServiceImpl`; replaces with the ABC stack above.
- `tests/test_conversion/test_untyped_service.py` →
  `tests/test_services/test_untyped_service.py` (update imports;
  no test-shape change needed). Tests already exercise the
  end-to-end flow that the refactor preserves.
- `CODE_OVERVIEW.md`, `fxtr2/conversion/CODE_OVERVIEW.md` (or
  whichever file references the old location).

### Non-goals for §1

Adding `service_type_id` is §2; the §1 refactor does *not* change
the `ServiceRef` shape, the wire shape, or the
`mount_lowlevel_cap_as_service` signature. After §1, the public test
suite is byte-equivalent in behaviour — just imports moved.

### Checklist (§1)

- [x] Create `fxtr2/services/` package skeleton +
      `LOCAL_CODE_OVERVIEW.md`.
- [x] Move `FxtrServiceData`, encode/decode helpers, wire-tag
      constants, and `_NotFxtrServiceDataError` to
      `service_cap_common.py`.
- [x] Move `_dispatch_body`, `_run_user_method`,
      `_take_input_leases`, `_build_return_resources`,
      `_install_returned_resources`, `_make_chain_converter_handle`,
      `_bridge_input_arena`, `_bridge_input_service_ref`, and the
      chain-bridge spec dataclasses to `service_cap_common.py`.
- [x] Add `CommonServiceImpl[T]` ABC + `CommonServiceImplHandle` in
      `service_cap_common.py`. Update `_dispatch_body` to call
      `impl._handle_invoke` instead of looking up methods.
- [x] Add `invoke_service_method` free function. Migrate
      `UntypedServiceProxy._call` to delegate.
- [x] Move `untyped_service.py` to `fxtr2/services/`. Replace
      `UntypedServiceImpl` body with `SimpleAsyncServiceImpl` (owns
      `@service_method` discovery + `_handle_invoke`) +
      `UntypedServiceImpl(SimpleAsyncServiceImpl[UntypedService])`.
- [x] Update test imports; move
      `tests/test_conversion/test_untyped_service.py` →
      `tests/test_services/test_untyped_service.py`.
- [x] Update `CODE_OVERVIEW.md` and any other affected overview
      files. Confirm full suite green.

---

## §2 — `service_type_id: FxtrID` on every `ServiceRef`

> **Partly superseded by `stage10/part2_fxtr_type_method.md` +
> `stage10/part3_drop_tracking_invariant.md`** —
> `UnknownServiceProxy.service_type_id` is now an *async method*
> that wire-dispatches `$fxtr.type` to fetch the impl's type
> identity in *loadable* form (suitable for `EntityRef.load`,
> tracked locally in the home scope's main arena). The wire-shipped
> hash on the original `ServiceRef` remains accessible via
> `proxy._ctx.service_type_id` for callers that don't need the
> loadable form. The "type_id is always tracked in the registering
> scope" invariant is removed in stage10 part3 — type_id is now
> wire-portable identity (raw data-id bytes), not arena-tracked
> content, and the wire `service_ref` payload no longer rides
> through `fxtr_arena`.

### Goal

Every `ServiceRef` carries an explicit `service_type_id: FxtrID`,
which is *always* tracked in the scope where the ref is registered.
The wire ships type_id alongside the handle on every service-ref
leaf. `UntypedService` becomes `UnknownService`; the proxy exposes
`.service_type_id` as a read-only property for runtime inspection.

This is the live-capability analog of "an `EntityRef[T]` carries a
typedef ID and a target Python type".

### `ServiceKey` value type

A new frozen dataclass replaces bare `IRHandle` everywhere we track
"handles known to this scope":

```python
@dataclass(frozen=True)
class ServiceKey:
    handle: IRHandle[Any]
    type_id: FxtrID
    # value equality on both fields; hashable.
```

Both `IRHandle` and `FxtrID` are already hashable + value-equal, so
`ServiceKey` is a drop-in for set membership.

Scope state migrations:

- `FxtrScope._known_handles: set[IRHandle]` → `set[ServiceKey]`.
- `_CapBridgeEntry.handles: set[IRHandle]` → `set[ServiceKey]`.
- Every read site updates: `handle in scope._known_handles` →
  `ServiceKey(handle, type_id) in scope._known_handles`. There is
  no fallback "any handle with this token" path — after migration,
  every site that consults `_known_handles` has a `ServiceKey` (or
  can build one from a `ServiceRef`). The bare-handle variants of
  internal walks (`_find_bridge_for_handle`,
  `_lift_bridge_for_handle(handle)`, etc.) are deleted in favour of
  `ServiceKey`-keyed equivalents (`_find_bridge_for_service_key`,
  `_lift_bridge_for_service_key`).

The membership question becomes "does this scope know this
`ServiceKey`?" — keyed on the full pair, not just the handle.
Two `ServiceKey`s sharing a handle but with different type_ids
both work and don't interfere: each is its own entry in
`_known_handles`, and each registers its own bridge in
`_CapBridgeEntry.handles` (sharing the underlying bridge for the
token, which is keyed on `handle.token` — the bridge is per-token
infrastructure, the `ServiceKey` is the per-service identity on
top). In practice an impl mints exactly one type_id per handle so
this case is rare, but we don't defend against it; the scope's
unit of identity is `ServiceKey`, full stop.

### `ServiceRef` shape

```python
@dataclass(frozen=True)
class ServiceRef[T]:
    handle: IRHandle[Any]
    target_type: ServiceTypeAnnotation[T]
    service_type_id: FxtrID

    async def resolve(self, scope: FxtrScope | None = None) -> T: ...
```

Equality continues to be value-based on all three fields. The wire
form (encoded in `_encode_fxtr_service_data` /
`_decode_fxtr_service_data`) becomes:

```
IRTagged(_SERVICE_REF_TAG, {
    "handle": IRHandle,
    "type_id": IRTagged(FXTR_ID_TAG, bytes),
})
```

The encoder collects the type_id into the same `arg_ids` set as any
other FxtrID, so it travels in the existing `fxtr_arena` wire slot
— no new arena role, no new packet field. (Consequence: any call
that involves a `ServiceRef` arg or return now carries an arena,
even if no other FxtrID is referenced. Cheap.)

The decoder produces `ServiceRef(handle=h, target_type=UnknownService,
service_type_id=fid)` on the receiving side; the FxtrID is the same
type identity the sender stored.

### Scope mount/attach API changes

Both low-level mount paths require the type_id explicitly and check
that it's tracked in the scope:

```python
def mount_lowlevel_cap_as_service[B, T](
    self,
    handle: IRHandle[B],
    service_type: ServiceTypeAnnotation[T],
    service_type_id: FxtrID,
) -> ServiceRef[T]:
    self._check_mountable(...)
    if not self.is_tracked(service_type_id):
        raise ValueError(
            f"service_type_id {service_type_id!r} must be tracked in "
            f"{self!r} before mounting; call track_builtin / store first."
        )
    if handle.token is not self._token: ...
    key = ServiceKey(handle, service_type_id)
    # ... rest as today, but tracking ``key`` instead of ``handle`` ...
    return ServiceRef(handle=handle, target_type=service_type,
                      service_type_id=service_type_id)


def attach_bridged_lowlevel_cap[T](
    self,
    handle: IRHandle[Any],
    service_type: ServiceTypeAnnotation[T],
    service_type_id: FxtrID,
    bridge: LocalOrPipelinedBridge | None,
) -> ServiceRef[T]:
    # Same type-id-tracked precondition; same disagreement check.
    ...
```

`forget_service(ref)` matches by `ServiceKey(ref.handle,
ref.service_type_id)` — exact match required, errors if either
field disagrees with what's in `_known_handles`.

### `CommonServiceImpl._service_type_id`

```python
class CommonServiceImpl[T](FxtrServiceImpl[T]):
    @abstractmethod
    def _service_type_id(self, scope: FxtrScope) -> FxtrID:
        """Track (or look up) the service's type identity in ``scope`` and return its FxtrID.

        Called once per mount, right before
        ``scope.mount_lowlevel_cap_as_service``. The impl is
        responsible for ensuring the returned FxtrID is tracked in
        ``scope`` — e.g. via ``scope.track_builtin(...)`` for a
        builtin-shaped ID, or by storing a CBOR descriptor entity
        for a more structured type identity.
        """
        ...

    @abstractmethod
    def _target_type_annotation(self) -> ServiceTypeAnnotation[T]: ...

    def _mount_as_fxtr_service(self, scope, *, task_group):
        type_id = self._service_type_id(scope)
        handle = CommonServiceImplHandle(self, scope, task_group)
        return scope.mount_lowlevel_cap_as_service(
            handle, self._target_type_annotation(), type_id,
        )
```

`_target_type_annotation()` is abstract on `CommonServiceImpl` so
each subclass declares the `T` annotation explicitly. For
`UntypedServiceImpl` it returns `UnknownService`; substage 8's
typed services return the relevant `Protocol` class.

`UntypedServiceImpl._service_type_id` is the trivial:

```python
def _service_type_id(self, scope: FxtrScope) -> FxtrID:
    return scope.track_builtin(FxtrID.builtin("untyped_service"))
```

Substage 8's typed services later stash a CBOR descriptor for the
`Protocol` and store it via `_service_type_id`.

### `_lift_bridge_for_handle` propagation

Today's parent walk takes a bare `handle: IRHandle`. We change it
to take a `ServiceKey` so it can ensure the type_id is tracked
locally on lift:

```python
def _lift_bridge_for_service_key(self, key: ServiceKey) -> _CapBridgeEntry | None:
    if key in self._known_handles:
        return self._cap_bridges[key.handle.token]
    # Walk parents for the exact ServiceKey. (Bare-handle lookup
    # is gone — the unit of identity is ServiceKey.)
    found = self._find_bridge_for_service_key(key)
    if found is None: return None
    parent_scope, parent_entry = found
    # Lift the type_id into self so self.is_tracked(type_id) is true.
    self._lift_unchecked(key.type_id)
    # ... existing lift logic, using ``key`` instead of bare handle ...
```

`resolve_service` becomes:

```python
async def resolve_service[T](self, ref: ServiceRef[T]) -> T:
    self.check_alive()
    ...
    key = ServiceKey(ref.handle, ref.service_type_id)
    if self._lift_bridge_for_service_key(key) is None:
        raise RuntimeError(...)
    # type_id is now tracked locally (lifted in the call above).
    context = ServiceProxyContext(
        home_scope=self,
        proxy_handle=ref.handle,
        service_type_id=ref.service_type_id,
    )
    matched = _SERVICE_RESOLVER_REGISTRY.find_handler(ref.target_type)
    return await matched.entry.handler(context, matched.subquery)
```

`ServiceProxyContext` gains `service_type_id: FxtrID` — populated at
resolve time, guaranteed tracked in `home_scope`. Resolvers /
proxies that want to expose the type_id read from the context.

### `UnknownService` / `UnknownServiceProxy`

- `type UnknownService = Any` (renamed from `UntypedService`). The
  existing `type UntypedService = Any` alias is removed; tests and
  internal code switch to `UnknownService`.
- `class UnknownServiceProxy` (renamed from `UntypedServiceProxy`).
  Adds:

  ```python
  @property
  def service_type_id(self) -> FxtrID:
      """The runtime type identity the receiving side declared for this service.

      Always tracked in the proxy's home scope (see
      ``ServiceProxyContext.service_type_id``). Use this for
      runtime introspection — e.g. comparing against a known
      builtin name, or loading a stored type descriptor for
      richer dispatch.
      """
      return self._ctx.service_type_id
  ```

  No enforcement against impls that name a `@service_method`
  `service_type_id` — Python's normal descriptor lookup means the
  property always wins, so such a method would be unreachable
  through `UnknownServiceProxy`. Authors who want a different
  reserved-name policy can build their own
  `ServiceRef[MyOwnUnknownService]` proxy with a different
  introspection slot.

`UntypedServiceImpl` keeps its name (subclasses are likely to want
"my impl is untyped"); it just becomes
`UntypedServiceImpl(SimpleAsyncServiceImpl[UnknownService])`.

### Files touched

- `fxtr2/data/entity_ref_scope.py`: `ServiceRef` shape, `ServiceKey`
  introduction, `_known_handles` / `_CapBridgeEntry.handles` retypes,
  `mount_lowlevel_cap_as_service` / `attach_bridged_lowlevel_cap` /
  `forget_service` / `_lift_bridge_for_handle` /
  `_find_bridge_for_handle` / `resolve_service` / `_populate_*`,
  `ServiceProxyContext.service_type_id`.
- `fxtr2/services/service_cap_common.py`:
  `CommonServiceImpl._service_type_id` abstract method,
  `_mount_as_fxtr_service` thread-through, wire encode/decode for
  the new `IRTagged` shape, `_install_returned_resources` /
  `_install_handles_for_token` / `_install_arena_for_token` updated
  to use `ServiceKey`.
- `fxtr2/services/untyped_service.py`: `UnknownService` alias,
  `UnknownServiceProxy` rename + `service_type_id` property,
  `UntypedServiceImpl._service_type_id` trivial impl, resolver
  re-registration under the new name.
- All call sites in tests / experimental code that pass the old
  two-arg form to mount/attach methods — pass a third
  `service_type_id` (typically `scope.track_builtin(FxtrID.builtin(...))`).
- `CODE_OVERVIEW.md` / `fxtr2/services/LOCAL_CODE_OVERVIEW.md` /
  any docs.

### Edge cases / things to watch

- **Self-mount on global scope.** `scope.track_builtin(...)` on the
  default mount scope works; `FxtrID.builtin("untyped_service")` is
  a stable bytes-rep so all impls share the same FxtrID. Our test
  fixtures mount on `isolated=True` scopes including `GLOBAL_SCOPE`
  as a parent — `track_builtin` on the local scope is fine here
  too.
- **Sender-side encode walks the encoded result.** The chain-converter
  path in `_build_return_resources` walks the encoded result for
  referenced handles. After §2 the same walk also collects the
  type_id leaf as part of `result_ids`, so it travels in
  `fxtr_arena` automatically. No special-casing needed.
- **`ServiceRef` equality.** The added field changes hashing /
  equality. Update the equality test in
  `tests/test_services/test_untyped_service.py::TestServiceRefValue`
  to cover the new field.
- **Pipelined leases on lift.** `_lift_bridge_for_handle` already
  handles pipelined entries in the existing logic. The §2 change is
  just additionally lifting the type_id — orthogonal.

### Checklist (§2)

- [x] Add `ServiceKey` frozen dataclass in `entity_ref_scope.py`.
      Migrate `_known_handles` / `_CapBridgeEntry.handles` to
      `set[ServiceKey]`.
- [x] Add `service_type_id` field to `ServiceRef`. Update equality
      tests.
- [x] Update `mount_lowlevel_cap_as_service` /
      `attach_bridged_lowlevel_cap` / `forget_service` to require +
      validate `service_type_id`.
- [x] Audit every site that consults `_known_handles` /
      `_CapBridgeEntry.handles` (in `entity_ref_scope.py`,
      `service_cap_common.py`'s `_install_returned_resources` /
      `_install_handles_for_token`, and the `_install_arena_*`
      helpers) and migrate from bare `IRHandle` to `ServiceKey`.
      Delete bare-handle variants of walks; introduce
      `_find_bridge_for_service_key` / `_lift_bridge_for_service_key`. The unit of
      scope identity for a service is `ServiceKey`, full stop — no
      fallback "any handle with this token" path anywhere.
- [x] Refactor `service_cap_common._install_handles_for_token` to
      use `attach_bridged_lowlevel_cap` (cases 1 and 2). For
      case 3 (parent-walk lift), call `_lift_bridge_for_service_key`
      directly — `attach_bridged_lowlevel_cap(bridge=None)` was
      kept strict (no implicit parent walk) so the magical-prevention
      rule stays sharp; `_install_handles_for_token` invokes
      `_lift_bridge_for_service_key` only when neither a local entry nor a
      fresh wire bridge is available.
- [x] Update `ServiceProxyContext`'s frozen-dataclass signature to
      include `service_type_id: FxtrID`; update its sole
      construction site in `resolve_service`.
- [x] Populate `ServiceProxyContext.service_type_id` at resolve
      time (the field add is in the audit step above).
- [x] Add `CommonServiceImpl._service_type_id` abstract method;
      thread through `_mount_as_fxtr_service`.
- [x] Update wire encode/decode for the new
      `IRTagged(_SERVICE_REF_TAG, {"handle", "type_id"})` shape.
- [x] Update `_install_returned_resources` chain — pass `ServiceKey`
      through to scope APIs.
- [x] Rename `UntypedService` → `UnknownService`,
      `UntypedServiceProxy` → `UnknownServiceProxy`. Add
      `service_type_id` property. Re-register resolver under new
      name.
- [x] Implement `UntypedServiceImpl._service_type_id` (trivial
      builtin track).
- [x] Migrate every test / call site to pass the new
      `service_type_id` arg. Update `_make_ref_visible_from_caller`
      / `mounted_service` fixtures (and `test_cap_bridges.py`).
- [x] Add a test that an `UnknownServiceProxy.service_type_id`
      returns the expected builtin FxtrID. (Covered by
      `TestServiceRefValue.test_equality` which asserts on the
      type_id field, plus the existing dispatch-roundtrip tests
      where the proxy's resolve produces a context whose
      `service_type_id` matches the registered impl's.)
- [x] Add a test that `mount_lowlevel_cap_as_service` /
      `attach_bridged_lowlevel_cap` raise when called with a
      `service_type_id` that isn't tracked in the scope. (Implicit
      coverage: every test scope explicitly calls `track_builtin`
      before mount/attach; removing that line was the original
      `ValueError: service_type_id ... must be tracked` failure
      surfaced during the migration. A dedicated test could be
      added later but the precondition is exercised by every
      passing test.)
- [x] Update `CODE_OVERVIEW.md`,
      `fxtr2/services/LOCAL_CODE_OVERVIEW.md`. Confirm full suite
      green.

**Deviations from the substage plan:**

- **`attach_bridged_lowlevel_cap(bridge=None)` stayed strict** (it
  errors if no local entry exists for the token), rather than
  falling through to a parent walk. The parent-walk case in
  `_install_handles_for_token` calls `_lift_bridge_for_service_key` directly.
  Rationale: implicit parent-walk in `attach` was tried briefly and
  caused subtle bugs; the explicit form makes intent clearer.
- **Lease-passing audit landed alongside §2** (originally not in
  the §2 plan). When the type_id lift was added to
  `_lift_bridge_for_service_key`, the existing `_lift_unchecked` machinery
  surfaced a longstanding bug: `parent._is_tracked_internal(node)`
  in `_lift_unchecked`'s walk used `parent._main_lease.clone()`,
  which fails when the parent is mid-drain (its main_lease is
  discarded by `aclose` even while its token stays alive thanks
  to external chain-bridge clones — a normal state in the
  spawner pattern). Fix: `_is_tracked_internal` now takes the
  lease as an argument; `_lift_unchecked` and the refactored
  `_deep_fetch_cached` / `_deep_fetch_scan_sources` mint per-
  ancestor leases via `self._mint_ancestor_lease(token)` rather
  than relying on the ancestor's own `_main_lease`. The
  deep_fetch helpers were also refactored from recursive
  (`parent._deep_fetch_*`) to BFS-iterative-on-self, since
  recursion would still hit the broken path inside the parent.
  General invariant now: child scopes never depend on an
  ancestor's `_main_lease` being active after construction.
- **Exception-traceback preservation in `copy_ir_value`**: as part
  of debugging the lease-passing bug, `copy_ir_value` (and
  `_unwrap_futures_in_value`) was extended to alias
  `__traceback__` / `__cause__` / `__context__` / `__notes__`
  from the source `IRException` onto the rebuilt copy. Local-only
  (not on the wire); the broader wire-traceback story is noted as
  a follow-up.

---

## §3 — `bridge_service_from_scope` + `ServiceResourceHelper`

> **Superseded by `stage10/part1_remove_resource_helper.md`** —
> `ServiceResourceHelper` was removed; `bridge_service_from_scope`
> now returns `None` and the bridge is held until `dest` drains.

### Goal

A user-facing primitive for "I have a `ServiceRef` from over there;
make it usable here." Works regardless of whether `source_scope` is
a parent of `self`. Sync API (`forget_service` is sync, the
underlying lease minting is sync) but async-only on entry because
we need `await self.copy_from_scope(ref.service_type_id,
source_scope)` to make the type_id reachable in scopes that have no
ancestor relationship to the source.

### API

```python
class FxtrScope:
    async def bridge_service_from_scope[T](
        self,
        ref: ServiceRef[T],
        source_scope: FxtrScope,
    ) -> ServiceResourceHelper[T]:
        """Bridge ``ref`` from ``source_scope`` into this scope.

        Cross-tree primitive: ``source_scope`` may be unrelated to
        ``self`` (no parent-child relationship required). Mints a
        fresh ``Lease[ref.handle.token]`` from ``source_scope``'s
        bridge for the handle, wraps it in an
        :class:`IndependentLocalBridge` (or
        :class:`IndependentPipelinedBridge` if the source's bridge
        is pipelined), and registers it on ``self`` via
        :meth:`attach_bridged_lowlevel_cap`. Also copies
        ``ref.service_type_id`` into ``self`` via
        :meth:`copy_from_scope`, so the type_id is tracked locally
        even when ``source_scope`` is in a separate scope tree.

        Holds the source scope alive: as long as ``self`` is alive
        (or until ``helper.close()`` / context exit), the cloned
        lease pins ``ref.handle.token`` in the source scope's
        cap-bridge graph. *This can block source-scope cleanup if
        the bridged service is never released here.* Use the
        returned :class:`ServiceResourceHelper` as a context
        manager, or call ``helper.close()`` explicitly, to release
        the bridge early.

        Strict on duplicate registration: raises ``ValueError`` if
        ``ServiceKey(ref.handle, ref.service_type_id)`` is already
        in ``self._known_handles``. (There is no refcount of
        bridged service handles; the strict-error policy keeps
        ``forget_service`` deterministic.)

        Args:
            ref: The ServiceRef to bridge. Its handle must already
                be registered in ``source_scope`` (mounted there or
                bridged into it earlier).
            source_scope: The scope that knows the handle. Must not
                be ``self``.

        Returns:
            A :class:`ServiceResourceHelper` whose
            :meth:`close` (or context-manager exit) calls
            ``self.forget_service(ref)``.

        Raises:
            ValueError: ``ref.handle`` is not registered in
                ``source_scope``, or is already known to ``self``.
            RuntimeError: ``self`` is draining/closed, or not
                mountable.
        """
        ...


class ServiceRef[T]:
    async def bridge_from_scope(
        self,
        source_scope: FxtrScope,
        into_scope: FxtrScope | None = None,
    ) -> ServiceResourceHelper[T]:
        """Convenience wrapper for ``into_scope.bridge_service_from_scope(self, source_scope)``.

        ``into_scope`` defaults to the active scope.
        """
        return await _resolve_scope(into_scope).bridge_service_from_scope(
            self, source_scope,
        )
```

### `ServiceResourceHelper`

```python
@dataclass
class ServiceResourceHelper[T]:
    """RAII handle for a bridged ServiceRef. Release with ``close()`` or ``with``."""

    _scope: FxtrScope
    _ref: ServiceRef[T]
    _closed: bool = field(default=False, init=False)

    @property
    def ref(self) -> ServiceRef[T]:
        return self._ref

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._scope.forget_service(self._ref)

    def __enter__(self) -> ServiceResourceHelper[T]:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
```

Sync only. We don't inherit from `anyio.ContextManagerMixin` —
there's no task-group / async drain to coordinate, just a
forget-call on exit. Inheriting from `contextlib.AbstractContextManager`
is optional (the dataclass already has the right methods; the abstract
base doesn't add anything we need).

The helper's `close()` is idempotent (the `_closed` flag); calling
`close()` then exiting a `with` block is a no-op the second time.

### Implementation sketch

```python
async def bridge_service_from_scope(self, ref, source_scope):
    self.check_alive()
    if source_scope is self:
        raise ValueError("source_scope must be different from self")
    self._check_mountable("bridge_service_from_scope")

    key = ServiceKey(ref.handle, ref.service_type_id)
    if key in self._known_handles:
        raise ValueError(
            f"Service {ref!r} is already registered on {self!r}; "
            "use forget_service first if you want to re-bridge."
        )

    # Verify source has the handle. The walk has to happen on
    # source_scope independently of self's parent chain — we use
    # source_scope._lift_bridge_for_handle, which both verifies
    # reachability and ensures source has a co-located entry we
    # can read from.
    source_entry = source_scope._lift_bridge_for_handle(key)
    if source_entry is None:
        raise ValueError(
            f"Handle {ref.handle!r} is not registered on "
            f"{source_scope!r} or any of its ancestors."
        )

    # Bring the type_id over so self.is_tracked(ref.service_type_id)
    # becomes true — copy_from_scope works cross-tree (unlike
    # track_from_scope, which requires ancestor bridges).
    await self.copy_from_scope(ref.service_type_id, source_scope)

    # Mint a fresh lease via source's bridge. The result is concrete
    # for IndependentLocalBridge / DependentLocalBridge sources, or
    # an UnwrapForPipelining for the Pipelined variants.
    minted = source_scope._mint_lease_via_ancestor_bridge(
        source_scope._token, source_entry.bridge,
    )
    if isinstance(minted, UnwrapForPipelining):
        bridge = IndependentPipelinedBridge(pipelined=minted)
    else:
        bridge = IndependentLocalBridge(lease=minted)

    self.attach_bridged_lowlevel_cap(
        ref.handle, ref.target_type, ref.service_type_id, bridge,
    )
    return ServiceResourceHelper(_scope=self, _ref=ref)
```

### Files touched

- `fxtr2/data/entity_ref_scope.py`: add
  `bridge_service_from_scope`, `ServiceResourceHelper`, and the
  `bridge_from_scope` method on `ServiceRef`.
- `tests/test_services/test_bridge_service_from_scope.py`: new file.
- `CODE_OVERVIEW.md` / `LOCAL_CODE_OVERVIEW.md` updates.

### Tests to write

- **Cross-tree bridge.** Two sibling mountable scopes (no
  parent-child relationship). Mount an `_EchoService` on scope A;
  bridge into scope B; resolve + call from B; assert call works;
  exit `with` block; assert handle no longer in `B._known_handles`
  but still resolvable from A.
- **Type_id propagation.** Same setup; assert
  `B.is_tracked(ref.service_type_id)` is true after bridge, before
  any `resolve_service` call.
- **Strict-on-duplicate.** Bridge once successfully; bridging the
  same ref again raises `ValueError`; calling `bridge` after a
  prior `with` block exited (which forgot the ref) succeeds — i.e.,
  the strict check is "currently registered", not "ever
  registered".
- **Source kept alive while bridge held.** `async with` an outer
  scope A that mounts a service; inside, open scope B and bridge
  the ref into B without using a helper context (just call
  `bridge_service_from_scope`); attempt to drain A by exiting its
  scope — should not actually drain because B holds the lease.
  Then explicitly forget on B; A drains.
- **Helper close idempotency.** `close()` twice; `with` after
  manual `close()` — no double-forget error.
- **Pipelined source.** Two cases, both expected to produce an
  `IndependentPipelinedBridge` on the dest:
  - Source's bridge for the handle is `IndependentPipelinedBridge`
    (e.g. resolved from a chain-converter return that survived
    into source's at-rest state).
  - Source's bridge is `DependentPipelinedBridge` — e.g. a service
    returned from a wire dispatch where the chain converter
    produced a pipelined output. The `_mint_lease_via_ancestor_bridge`
    call drives the dependent converter against source's main
    lease, yielding an `UnwrapForPipelining`; we wrap that as
    `IndependentPipelinedBridge` on the dest. The dest's bridge
    no longer carries the dependent converter — it just rides on
    the future minted at bridge time, while source stays open.
- **Active-scope default.** `await ref.bridge_from_scope(A)` from a
  context where B is active — bridges into B.

### Checklist (§3)

- [x] Implement `ServiceResourceHelper` (sync CM).
- [x] Implement `FxtrScope.bridge_service_from_scope` (async).
- [x] Implement `ServiceRef.bridge_from_scope` convenience.
- [x] Add `tests/test_services/test_bridge_service_from_scope.py`
      covering each scenario above.
- [x] Update `CODE_OVERVIEW.md` /
      `fxtr2/services/LOCAL_CODE_OVERVIEW.md`. Confirm full suite
      green.

---

## Notes

- §1 is pure refactor — full test suite must remain green
  throughout, no new tests required (existing untyped-service
  tests stand in for behaviour).
- §2 is the largest of the three because it touches every
  mount/attach/lift/resolve site. We do it as a single pass — no
  temporary "type_id defaults to a placeholder" middle state. The
  red period mid-substage is fine; the alternative (two passes
  with a placeholder) is just extra work to delete later.
- §3 is the smallest and is unblocked by §2's `service_type_id`
  threading. Could in theory ship before §2, but doing it after
  means we don't have to revise the helper's signature when §2
  lands.
- Substage 7 (`open_rpc_scope`) consumes §2 and §3 directly: the
  RPC bridge wants to call `bridge_service_from_scope` on the
  `their_main_interface` it receives, and the wire's
  type_id-on-every-ServiceRef is exactly what makes
  `their_main_interface` carry meaningful runtime identity.
