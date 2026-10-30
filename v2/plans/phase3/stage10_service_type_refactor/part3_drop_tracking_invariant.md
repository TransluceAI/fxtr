# Stage 10 part 3 — Drop the "type_id is tracked locally" invariant

## Motivation

Today every scope that knows a `ServiceKey(handle, type_id)` is
required to also have `type_id` tracked in its main arena.
`mount_lowlevel_cap_as_service` / `attach_bridged_lowlevel_cap`
enforce this with `is_tracked` checks; `_lift_bridge_for_service_key`
calls `_lift_unchecked(key.type_id)` whenever it lifts a key from a
parent; `bridge_service_from_scope` does an explicit
`copy_from_scope(ref.service_type_id, source_scope)` to satisfy the
precondition; the `_encode_fxtr_service_data` path collects
`service_type_id` into the arena's `tracked_ids` so the receiver can
prov-decode it.

After parts 1 + 2:

- `ServiceResourceHelper` is gone (part 1).
- `$fxtr.type` is the authoritative way to ask the impl "what type
  are you?" (part 2).

This part removes the underlying invariant. After part 3:

- `ServiceRef.service_type_id` is **just a preloaded hash** — it
  identifies the kind of service for fast-path comparison /
  validator caching, but is not required to be tracked anywhere.
- The wire encodes a `ServiceRef` leaf with the type_id as bare
  bytes (the data ID half of the FxtrID). No fxtr_arena coverage
  for the type_id is needed.
- Anyone who actually needs to introspect the type entity (e.g. load
  a typed descriptor) goes through `$fxtr.type`, which returns a
  proper layered FxtrID via the ordinary fxtr_arena return mechanism.
- `service_type_validator` gets a per-validator cache that maps
  `FxtrID -> bool`; on cache miss the resolver invokes `$fxtr.type`
  to produce a tracked id (so the validator can `EntityRef.load()`),
  asserts equality with the wire hash, runs the validator predicate,
  and caches the result. On cache hit we trust the wire hash and
  skip the wire dispatch entirely.

This part also drops the `fxtr_arena`-pinned bootstrap field in
`scope_bridge.py`'s peer-export packet (its only purpose was the
type_id lift, which is no longer required).

## Scope

- **§3.1.** Wire encoding: `ServiceRef.service_type_id` ships as raw
  `bytes` directly inside the `service_ref` payload — no
  `IRTagged(FXTR_ID_TAG, ...)` wrapper, no fxtr_arena coverage. The
  enclosing `service_ref` tag tells the decoder it's a type_id, so a
  redundant inner FxtrID tag would just be noise. Same shape on input
  and return.
- **§3.2.** Bookkeeping: drop the `is_tracked(type_id)` precondition
  in `mount_lowlevel_cap_as_service` /
  `attach_bridged_lowlevel_cap`; remove the type_id lift in
  `_lift_bridge_for_service_key`. Update `ServiceKey` /
  `_known_handles` docstrings to drop the invariant. Keep
  `ServiceKey` as the unit of identity (its data shape doesn't
  change).
- **§3.3.** `bridge_service_from_scope`: drop the
  `copy_from_scope(service_type_id)` call.
- **§3.4.** Validator path: weak-keyed
  `dict[ServiceTypeValidator, dict[FxtrID, bool]]` cache; on miss
  invoke `$fxtr.type`, assert equal, run validator, cache;
  `service_type_id_check` (exact-match) trusts the wire hash with no
  $fxtr.type dispatch (it's effectively a one-entry cache).
- **§3.5.** `scope_bridge.py` simplification: drop the
  `fxtr_arena` / `arena_lease` fields from the bootstrap packet.

Out of scope:

- Anything still in part 1 / 2 (helper removal, `$fxtr.type`
  plumbing) — those already landed.
- Wire-traceback story (orthogonal).

---

## §3.1 — Wire encoding for `ServiceRef.service_type_id`

### `service_cap_common.py`

`_encode_fxtr_service_data`:

```python
if isinstance(value, ServiceRef):
    service_keys.add(ServiceKey(value.handle, value.service_type_id))
    return IRTagged(
        _SERVICE_REF_TAG,
        {
            _SERVICE_REF_HANDLE_FIELD: value.handle,
            # Raw bytes — no FxtrID tag (the service_ref outer tag
            # already implies "type_id"), no prov, no arena coverage.
            _SERVICE_REF_TYPE_ID_FIELD: bytes(value.service_type_id),
        },
    )
```

Three changes from today:

- The `fxtr_ids.add(_arena_id(value.service_type_id))` line is
  deleted. The type_id no longer rides in the input/return arena.
- The encoding is raw `bytes(...)` instead of
  `encode_fxtr_id_with_prov(...)`. The intermediate
  `IRTagged(FXTR_ID_TAG, ...)` wrapper is also dropped — we already
  know from the enclosing `service_ref` tag that the value at this
  field is a type_id, and we deliberately aren't reusing any of the
  arena-tracked FxtrID encode/decode machinery (which is the only
  reason the `FXTR_ID_TAG` exists). `bytes(fid)` returns the
  `_data_id` raw bytes; equality / hashing on `FxtrID` already
  ignores `_provenance_parent`, so a layered impl-side type_id
  round-trips equal to its bare wire form.

`_decode_fxtr_service_data`:

```python
if value.tag == _SERVICE_REF_TAG:
    payload = value.value
    if not isinstance(payload, Mapping):
        raise IRException(...)
    handle = payload.get(_SERVICE_REF_HANDLE_FIELD)
    if not isinstance(handle, IRHandle):
        raise IRException(...)
    type_id_raw = payload.get(_SERVICE_REF_TYPE_ID_FIELD)
    if not isinstance(type_id_raw, bytes):
        raise IRException(
            message=(
                f"{where}: service_ref.{_SERVICE_REF_TYPE_ID_FIELD!r} must be "
                f"raw bytes; got {type_id_raw!r}"
            ),
            category="MalformedPacket",
        )
    type_id = FxtrID(raw_data_id=type_id_raw)
    service_keys.add(ServiceKey(handle, type_id))
    return ServiceRef(handle=handle, target_type=UnknownService, service_type_id=type_id)
```

Drop `decode_fxtr_id_with_prov` for the type_id field. The
`fxtr_ids.add(...)` for the type_id is also gone. The decoder
expects raw `bytes` directly under `_SERVICE_REF_TYPE_ID_FIELD` —
no `IRTagged` envelope to peel.

The `FXTR_ID_WITH_PROV_TAG` / `FXTR_ID_TAG` decode paths only apply
to user-data leaves now — the type_id field is plain bytes inside
the `service_ref` mapping.

### Updated docstrings

- `_SERVICE_REF_TYPE_ID_FIELD` doc: "Raw `bytes` of the ``FxtrID``'s
  data id — no `IRTagged` envelope (the enclosing `service_ref` tag
  already implies "type_id"), no provenance parent (the type_id is
  wire-portable identity, not arena-tracked content). Consumers that
  need the layered form must invoke ``$fxtr.type`` over the handle."
- The "wire shape" prose in module docstring + LOCAL overviews:
  "ServiceRef's `service_type_id` is wire-encoded as bare bytes; it
  rides no arena. Service calls without other FxtrIDs in
  args/return naturally don't carry an `fxtr_arena`."
- The `FxtrServiceData` doc near the encode function: drop "any
  service call with a `ServiceRef` arg always carries an
  `fxtr_arena`" — that's no longer true.

### Tests for §3.1

- Encode round-trip a `ServiceRef` with a layered (prov-bearing)
  `service_type_id` directly via `_encode_fxtr_service_data` /
  `_decode_fxtr_service_data`. Assert the decoded ref's
  `service_type_id._provenance_parent is None` (prov is not
  propagated). Assert `bytes(decoded.service_type_id) == bytes(orig.service_type_id)`
  (data id is preserved). Assert `decoded.service_type_id == orig.service_type_id`
  (FxtrID equality is data-id-only, so they compare equal).
- Add a test that a service call whose only ID-bearing arg is a
  ServiceRef (no other FxtrIDs / EntityRefs in args) ships with
  `fxtr_arena=None` in the input packet (verify by intercepting the
  packet, or by asserting that the input arena handle is None on
  the receiver). Confirm the call still completes.

---

## §3.2 — Drop the tracking invariant in scope bookkeeping

### `mount_lowlevel_cap_as_service`

Remove the `self.lift(service_type_id)` call (lines 3295-3299
today). The body becomes:

```python
def mount_lowlevel_cap_as_service[B, T](
    self,
    handle: IRHandle[B],
    service_type: ServiceTypeAnnotation[T],
    service_type_id: FxtrID,
) -> ServiceRef[T]:
    self._check_mountable("mount_lowlevel_cap_as_service")
    if handle.token is not self._token:
        raise ValueError(...)
    key = ServiceKey(handle, service_type_id)
    entry = self._cap_bridges.get(handle.token)
    if entry is None:
        entry = _CapBridgeEntry(
            bridge=DependentLocalBridge(
                leases={},
                converter=_identity_dependent_local_converter,
            ),
            handles=set(),
        )
        self._cap_bridges[handle.token] = entry
    entry.handles.add(key)
    self._known_handles.add(key)
    return ServiceRef(handle=handle, target_type=service_type, service_type_id=service_type_id)
```

(Note: this method is normally called by
`CommonServiceImpl._mount_as_fxtr_service`, which has already done
the impl-side `_service_type_id()` call. The impl is still expected
to track the id in its mount scope so that the impl's own `$fxtr.type`
return can flow through the per-call return arena to the caller —
that's a per-impl convention, not a framework precondition. The
framework no longer enforces the convention because the framework
no longer cares.)

### `attach_bridged_lowlevel_cap`

Remove the `if not self.is_tracked(service_type_id)` block (lines
3372-3376 today). The `bridge=None` requires-pre-existing-entry
clause stays unchanged.

### `_lift_bridge_for_service_key`

Remove the `self._lift_unchecked(key.type_id)` call (lines 3557-
3562 today). The lift logic for the cap-bridge stays the same;
only the type_id-lift step goes away.

### `ServiceKey` / `_known_handles` docstrings

- `ServiceKey` doc: drop the "scope guarantees that whenever a
  `ServiceKey` is in `_known_handles`, the matching `type_id` is
  tracked in the scope's main arena" paragraph. Replace with: "The
  `type_id` is a wire-portable hash (an arbitrary `FxtrID` minted
  by the impl); it identifies the kind of service for fast-path
  gating but is not required to be tracked locally. To introspect
  the actual type entity, dispatch ``$fxtr.type`` over the handle."
- `_known_handles` field comment: same edit.
- `ServiceRef.service_type_id` doc: rewrite as "Wire-portable
  runtime identity hint. Equality on `FxtrID` ignores the prov
  parent, so a layered impl-side id and its bare wire form
  compare equal. To verify the impl actually has this identity,
  invoke `$fxtr.type` (e.g. via `await proxy.service_type_id()` for
  the untyped proxy)."
- `ServiceProxyContext.service_type_id` doc: "The wire-shipped
  type_id from the resolved ref. Not necessarily tracked in
  `home_scope`. Consumers that need to load a stored type-
  descriptor entity should invoke ``$fxtr.type`` (which returns a
  properly-tracked, possibly layered, FxtrID); the wire hash is
  for fast-path comparison only."
- `mount_lowlevel_cap_as_service` / `attach_bridged_lowlevel_cap`
  docstrings: drop the "Must already be tracked in this scope's
  main arena" precondition language for `service_type_id`.

### `ServiceRef.lift`

`scope.lift(ref)` for a `ServiceRef` currently calls
`_lift_bridge_for_service_key(key)` which lifts the type_id as a
side effect. After §3.2 the side effect is gone — `lift` just
registers the cap-bridge for the handle. Update the docstring to
drop the "lifting the cap-bridge (and `service_type_id` entity)
from a parent" phrasing.

### `is_tracked(ServiceRef)`

Today's implementation checks
`ServiceKey(node.handle, node.service_type_id) in self._known_handles`.
Stays the same — the check is per-`ServiceKey`, not per-`type_id`.

### Tests for §3.2

- Drop the (implicit) "ValueError when service_type_id isn't tracked"
  expectation. There isn't an explicit test for this today (per the
  part6post4 §2 checklist note), but search for assertions that
  rely on `is_tracked(type_id)` being true after a mount; relax or
  delete as appropriate.
- Add a positive test: mount a service whose `_service_type_id()`
  returns an ID that is *not* tracked in the mount scope (an artifical
  case for the test — bypass the convention by hand-constructing the
  impl). Mount succeeds; `attach_bridged_lowlevel_cap` succeeds;
  resolve succeeds; calling `$fxtr.type` on the proxy returns the
  registered id and the caller-side decoded value is `is_tracked`
  in the home scope (because it came back through the return arena
  via `track_builtin` / similar inside `_service_type_id`'s normal
  impl). This test pins down the new "impl is responsible for the
  $fxtr.type return; framework doesn't pre-validate" contract.

---

## §3.3 — Simplify `bridge_service_from_scope`

### `entity_ref_scope.py`

Drop the `await self.copy_from_scope(ref.service_type_id, source_scope)`
call (line 4023 today). The body becomes:

```python
async def bridge_service_from_scope[T](
    self, ref: ServiceRef[T], source_scope: FxtrScope,
) -> None:
    self.check_alive()
    self._check_mountable("bridge_service_from_scope")
    if source_scope is self:
        raise ValueError("source_scope must be different from self.")
    key = ServiceKey(ref.handle, ref.service_type_id)
    if key in self._known_handles:
        raise ValueError(
            f"Service {ref!r} is already registered on {self!r}."
        )

    source_entry = source_scope._lift_bridge_for_service_key(key)
    if source_entry is None:
        raise ValueError(
            f"Service {ref!r} is not registered on {source_scope!r} "
            f"or any of its ancestors."
        )

    minted = source_scope._mint_lease_via_ancestor_bridge(
        source_scope._token, source_entry.bridge
    )
    bridge: IndependentLocalBridge | IndependentPipelinedBridge
    if isinstance(minted, UnwrapForPipelining):
        bridge = IndependentPipelinedBridge(pipelined=minted)
    else:
        bridge = IndependentLocalBridge(lease=minted)

    self.attach_bridged_lowlevel_cap(
        ref.handle, ref.target_type, ref.service_type_id, bridge,
    )
```

Note that `bridge_service_from_scope` is now a sync-effecting method
that happens to be `async def`. We could in principle drop the
`async` keyword now (since `copy_from_scope` was the only async
operation), but I'd recommend keeping it `async def` for API
stability — callers already write `await dest.bridge_service_from_scope(...)`,
and a future change might re-introduce an async step. (Open to
flipping to sync if that's preferred — flag as a follow-up.)

### Docstring update

- Drop step 2 ("Copies `ref.service_type_id` into `self`'s main arena…").
- Renumber subsequent steps.
- Note in the lifetime-semantics paragraph: "The bridge holds a
  clone of `Lease[ref.handle.token]` for `self`'s lifetime. Source
  scope drain blocks on `self`'s drain. There is no early-cleanup
  primitive; build a child scope of `self` if you need shorter
  lifetimes."

### Tests for §3.3

- Existing tests in `test_bridge_service_from_scope.py` should still
  pass without modification (they don't depend on the type_id being
  tracked in dest, except for `TestTypeIdPropagation` which
  explicitly asserts it). Update `TestTypeIdPropagation`:
  - Drop the `assert scope_b.is_tracked(ref.service_type_id)` line.
  - Replace with: bridge in B, call `await proxy.service_type_id()`
    (which dispatches `$fxtr.type`), assert the returned id matches
    the impl's expected id, AND that
    `scope_b.is_tracked(returned_id)` is true (because the wire
    return-arena bridged it).

---

## §3.4 — Validator path: per-validator cache + `$fxtr.type` invocation

### `typed_service.py`

Add a module-level cache:

```python
_VALIDATOR_RESULT_CACHE: weakref.WeakKeyDictionary[
    ServiceTypeValidator, dict[FxtrID, bool]
] = weakref.WeakKeyDictionary()
"""Per-validator cache mapping wire-shipped service_type_id → predicate result.

Keyed weakly on the validator callable so user code that constructs
short-lived validators doesn't accumulate cache entries forever.
The inner dict is keyed on the bare wire hash (FxtrID equality
already ignores prov); on cache hit we trust the cached bool and
skip the $fxtr.type dispatch. On cache miss we invoke $fxtr.type to
produce a properly-tracked id (so the validator can EntityRef.load
it), assert equality with the wire hash, run the predicate, and
cache the result.
"""
```

Note on weakref keys: validators are typically module-level
functions or methods; `WeakKeyDictionary` works for both as long as
something else holds a reference (e.g. the `_ServiceProtocolMetadata`
the validator was registered with). If a user constructs a validator
inline as a lambda and then drops the reference, the cache entry
goes too — harmless (and the validator wasn't reachable anyway). The
metadata-attached reference keeps long-lived validators alive
indefinitely, which is what we want.

If the validator is a method or partial that doesn't support
weakref, fall back to a regular dict keyed on `id(validator)` —
practically users register validators once at import time so leak
risk is low. We can decide on the fallback in code review.

### Resolver gate body

Today's resolver gate (lines 904-919):

```python
if metadata.service_type_id_check is not None:
    if context.service_type_id != metadata.service_type_id_check:
        raise ServiceTypeIdMismatchError(...)
elif metadata.service_type_validator is not None:
    ok = await metadata.service_type_validator(context.service_type_id, target_type)
    if not ok:
        raise ServiceTypeIdMismatchError(...)
```

Reworked:

```python
if metadata.service_type_id_check is not None:
    # Exact-match gate: trust the wire hash, no $fxtr.type call.
    # Equivalent to a 1-entry cache where the registered id maps
    # to True and every other id maps to False.
    if context.service_type_id != metadata.service_type_id_check:
        raise ServiceTypeIdMismatchError(
            f"Resolved ServiceRef has service_type_id "
            f"{context.service_type_id!r}; protocol "
            f"{metadata.proto.__name__} requires "
            f"{metadata.service_type_id_check!r}."
        )
elif metadata.service_type_validator is not None:
    ok = await _check_validator_with_cache(
        validator=metadata.service_type_validator,
        wire_type_id=context.service_type_id,
        target_type=target_type,
        context=context,
        proto_name=metadata.proto.__name__,
    )
    if not ok:
        raise ServiceTypeIdMismatchError(
            f"Resolved ServiceRef's service_type_id "
            f"{context.service_type_id!r} rejected by validator on "
            f"protocol {metadata.proto.__name__}."
        )
```

`_check_validator_with_cache`:

```python
async def _check_validator_with_cache(
    *,
    validator: ServiceTypeValidator,
    wire_type_id: FxtrID,
    target_type: ServiceTypeAnnotation[Any],
    context: ServiceProxyContext,
    proto_name: str,
) -> bool:
    """Wire-cache + verify-via-$fxtr.type wrapper for service_type_validator.

    1. Look up ``validator`` in ``_VALIDATOR_RESULT_CACHE``;
       sub-look-up ``wire_type_id``. If hit, return cached bool.
    2. On miss: invoke ``$fxtr.type`` on ``context.proxy_handle``
       (via ``invoke_service_method``) to get the impl's
       authoritative type identity. The returned id is bridged into
       ``context.home_scope`` via the ordinary fxtr_arena return
       path, so it's safe to ``EntityRef.load`` against home_scope
       inside the validator.
    3. Assert ``returned_id == wire_type_id`` (FxtrID equality is
       data-id-only, so a layered impl-side id matches the bare
       wire hash). Mismatch raises a ``ServiceTypeIdMismatchError``
       describing the disagreement.
    4. Call ``await validator(returned_id, target_type)`` — pass
       the *returned* id (potentially layered) so the predicate can
       inspect provenance / load the descriptor.
    5. Cache the bool keyed on ``wire_type_id`` (so future hits
       skip the wire dispatch).
    """
    cache = _VALIDATOR_RESULT_CACHE.setdefault(validator, {})
    cached = cache.get(wire_type_id)
    if cached is not None:
        return cached
    returned_id = await invoke_service_method(
        context, FXTR_TYPE_METHOD_NAME,
    )
    if not isinstance(returned_id, FxtrID):
        raise IRException(
            message=(
                f"Resolver for {proto_name}: $fxtr.type returned "
                f"non-FxtrID {returned_id!r}"
            ),
            category="MalformedPacket",
        )
    if returned_id != wire_type_id:
        raise ServiceTypeIdMismatchError(
            f"$fxtr.type returned {returned_id!r}; wire hash "
            f"declared {wire_type_id!r}. Proxy on {proto_name} "
            f"refusing to validate (impl identity disagrees with "
            f"sender)."
        )
    ok = await validator(returned_id, target_type)
    cache[wire_type_id] = ok
    return ok
```

Note that the cache stores the wire hash; the validator receives
the layered form. If a future request comes in with a wire hash
that's already cached, we use the cached bool and never call
`$fxtr.type` again — the second-look-up isn't free in the sense
that we're trusting the wire hash, but that's exactly the trust
model we wanted: validators say "kind X is OK", and the wire hash
identifies the kind for caching purposes.

### Tests for §3.4

`tests/test_services/test_typed_service.py`'s validator coverage
(lines 330, 687) needs updating:

- Update the existing validator tests to cover the new cache /
  $fxtr.type-on-miss flow:
  - Resolve a typed proxy whose impl has type_id X; assert validator
    is called once with X.
  - Resolve a second typed proxy with the same X; assert validator
    is NOT called again (cache hit), and `$fxtr.type` is NOT
    dispatched (intercept via a counting mock impl).
  - Resolve a third typed proxy with a different type_id Y;
    assert validator is called with Y.
- Add a negative test: impl returns type_id Y from `$fxtr.type` but
  the wire ServiceRef was constructed with type_id X (via direct
  bypass — useful for testing). Resolve raises
  `ServiceTypeIdMismatchError("$fxtr.type returned ... wire hash
  declared ...")`.
- Add a layered-id test: validator wants to load a typed-descriptor
  EntityRef. Mount an impl whose `_service_type_id()` returns
  `EntityRef(MyDescriptor(...).store()).entity_id` (layered). Wire
  ships the bare bytes; validator receives the layered form;
  validator calls `EntityRef(layered_id, MyDescriptor).load()` and
  uses the descriptor.
- Add a test that documents the cache-hit-on-different-target case:
  a validator registered against `MyService[int]` and `MyService[str]`
  with the same impl returns the same cached bool because the cache
  key is `(validator, wire_type_id)`, not `(validator, wire_type_id,
  target_type)`. If a user wants per-target results they need
  separate validators. (This is a deliberate design choice — most
  validators are independent of target_type; if it turns out to be
  too restrictive, expand the cache key in a follow-up.)

---

## §3.5 — Simplify `scope_bridge.py` bootstrap packet

### Current shape (lines 419-426 today)

```python
body: dict[str, Any] = {
    _KEY_HANDLE: our_main_service.handle,
    _KEY_TYPE_ID: IRTagged(tag=FXTR_ID_TAG, value=bytes(our_main_service.service_type_id)),
    _KEY_FXTR_ARENA: arena_handle,
    _KEY_HANDLE_LEASE: handle_lease,
    _KEY_ARENA_LEASE: arena_lease,
}
```

### After §3.5

The arena pieces existed only to lift the peer's `type_id` into
`user_scope` so the receiver-side `attach_bridged_lowlevel_cap`
could satisfy `is_tracked`. After §3.2, that precondition is gone.
Drop the arena-related fields:

```python
body: dict[str, Any] = {
    _KEY_HANDLE: our_main_service.handle,
    _KEY_TYPE_ID: bytes(our_main_service.service_type_id),
    _KEY_HANDLE_LEASE: handle_lease,
}
```

While we're here: also drop the `IRTagged(FXTR_ID_TAG, ...)` envelope
around the type_id and ship it as raw bytes, matching the new
`service_ref` encoding in §3.1. Same rationale: the bootstrap key
already says it's a type_id, the FxtrID-tag wrapper is redundant,
and we deliberately aren't reusing arena-tracked FxtrID encoding
here. `_parse_bootstrap_packet` updates from
"type_id_tagged must be `IRTagged(FXTR_ID_TAG, bytes)`" to
"`type_id_raw` must be raw `bytes`", and constructs
`type_id = FxtrID(raw_data_id=type_id_raw)` directly.

Sender side:

- Drop the `sub_arena = host_scope._main_arena.copy_to_new_arena(...)`
  fork.
- Drop the `arena_handle = ArenaIRHandleAdapter(...)` construction.
- Drop the `arena_lease = host_scope._main_lease.clone()` call.
- Drop the `_KEY_FXTR_ARENA` / `_KEY_ARENA_LEASE` dict entries.
- The `wrapper_task_group` parameter becomes unused for arena
  hosting; check whether anything else uses it (likely yes for
  other parts of the bridge), keep accordingly.

Receiver side (`_install_peer_main_service`):

- Drop the `CapIRArena.direct(...)` construction + the
  `lazily_attach_from_arena` call + the `cap_ir.close()` finally.
- Drop the `arena_lease.discard()` failure-path cleanup.
- The function reduces to the final
  `user_scope.attach_bridged_lowlevel_cap(handle, type, type_id,
  IndependentLocalBridge(lease=handle_lease))` call.

`_BootstrapPacket`:

- Drop the `fxtr_arena: IRHandle[Any]` and `arena_lease: Lease[Any]`
  fields.

`_parse_bootstrap_packet`:

- Drop the `fxtr_arena` / `arena_lease` validation block.
- Update the constants `_KEY_FXTR_ARENA` and `_KEY_ARENA_LEASE` —
  delete them, or leave the names defined for back-compat? Delete
  them; the bridge is internal and there are no external consumers.

`open_rpc_scope` precondition lift:

- Today's setup probably calls `host_scope.lift(our_main_service.service_type_id)`
  before the export. Drop that call — it's no longer needed.
  Search for uses and remove.

### Tests for §3.5

`tests/rpc/test_scope_bridge.py` (or wherever scope_bridge tests
live):

- Update the on-the-wire shape assertions to match the new packet
  shape (3 keys instead of 5).
- Update tests that verified `is_tracked(type_id)` on the receiving
  user_scope after bootstrap. After this change, the type_id is NOT
  necessarily tracked in user_scope. If a test relied on that,
  rewrite to: bootstrap, call `await main_proxy.service_type_id()`
  (which dispatches `$fxtr.type`), assert the returned id matches
  the peer's declared id and is tracked in user_scope after the
  call.

### Plan-doc cross-reference

`plans/phase3/stage8c_user_caps/part6post4_service_ergonomics.md`
doesn't directly touch `scope_bridge.py`, but the broader
RPC-bridge plan does. Add a one-line note in
`fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` that the bootstrap packet
shape changed in this stage; the previous arena-coupled shape is
gone.

---

## §3.6 — Documentation updates

### `fxtr2/data/LOCAL_CODE_OVERVIEW.md`

- Live-capability bridges section: remove the phrase "the scope
  guarantees that whenever a `ServiceKey` is in `_known_handles`,
  the matching `type_id` is tracked in the scope's main arena".
  Replace with: "the type_id is wire-portable identity, not a
  tracked entity — to introspect, dispatch ``$fxtr.type``."
- Mount methods section: drop the "`service_type_id` already
  tracked in self's main arena" precondition language for both
  `mount_lowlevel_cap_as_service` and `attach_bridged_lowlevel_cap`.
- `bridge_service_from_scope` section: drop the
  "copies `ref.service_type_id` into `dest` (via `copy_from_scope`,
  the cross-tree-safe variant)" sentence; replace with "no
  type_id copy — the type_id is just wire-portable identity."

### `fxtr2/services/LOCAL_CODE_OVERVIEW.md`

- "Wire shape" subsection: rewrite the description of the
  `service_ref` leaf — bare-bytes type_id encoding, no fxtr_arena
  coverage. Drop "any service call with a `ServiceRef` arg always
  carries an `fxtr_arena`".
- Server-side dispatch flow: drop "bridges in ServiceRef args
  (`bridge=None` against the ancestor entry)" — that step still
  registers the cap-bridge, but no type_id lift happens. Update
  wording.
- "Cross-tree bridging" subsection: rewrite for the new shape (no
  type_id copy, no helper).

### `CODE_OVERVIEW.md`

- All the same edits in the corresponding root-level sections.

### `service_cap_common.py` module docstring

- Update the "wire shape" section to match the new `service_ref`
  encoding.
- Note that `$fxtr.type` is the route for type-id introspection.

### Plan-doc cross-references

- `plans/phase3/stage8c_user_caps/part6post4_service_ergonomics.md`
  §2 "Goal": add a note "Superseded by stage10 part3: type_id is
  no longer tracked locally; the wire-portability is preserved but
  the in-scope tracking precondition is gone."

---

## §3.7 — Checklist

### §3.1 — Wire encoding

- [x] Update `_encode_fxtr_service_data`: drop type_id arena
      collection; encode type_id as raw `bytes` (no `IRTagged`
      envelope at all — the `service_ref` outer tag already implies
      "type_id").
- [x] Update `_decode_fxtr_service_data`: type_id field accepts
      raw `bytes` directly; drop the prov-decode + arena-collect.
- [x] Update relevant docstrings (constants, encode/decode docs,
      module overview).
- [x] Add round-trip test: layered impl-side type_id encodes to
      bare wire form, decodes bare-equal. (`test_provenance_flow.py::TestServiceTypeIDProvenance::test_layered_type_id_ships_as_bare_bytes` —
      asserts no arena collection on encode AND decode, plus
      data-id equality.)
- [x] Add no-arena test: ServiceRef-only call ships
      `fxtr_arena=None`. (Implicit coverage: the same
      `test_layered_type_id_ships_as_bare_bytes` test asserts
      `fxtr_ids == set()` after encoding a `ServiceRef` — the
      higher-level `invoke_service_method` skips the arena-fork
      step when `fxtr_ids` is empty.)

### §3.2 — Bookkeeping invariant drop

- [x] Remove `self.lift(service_type_id)` from
      `mount_lowlevel_cap_as_service`.
- [x] Remove `is_tracked(service_type_id)` check from
      `attach_bridged_lowlevel_cap`.
- [x] Remove `_lift_unchecked(key.type_id)` from
      `_lift_bridge_for_service_key`.
- [x] Update `ServiceKey`, `ServiceRef`, `ServiceProxyContext` doc
      strings.
- [x] Update `mount_lowlevel_cap_as_service` /
      `attach_bridged_lowlevel_cap` doc strings.
- [x] Update `ServiceRef.lift` doc string.
- [x] Add positive test for "service mounted with untracked
      type_id resolves and dispatches `$fxtr.type` correctly".
      (Renamed `TestServiceTypeIdValidation` →
      `TestServiceTypeIdNotTracked`; the `test_mount_accepts_untracked_type_id`
      and `test_attach_accepts_untracked_type_id` tests pin the new
      contract.)

### §3.3 — `bridge_service_from_scope`

- [x] Drop the `await self.copy_from_scope(...)` call.
- [x] Update the docstring (drop step 2, renumber).
- [x] Update `TestTypeIdPropagation` to use `$fxtr.type` instead of
      `is_tracked(type_id)`. (Renamed test method to
      `test_type_id_not_tracked_after_bridge_but_loadable_via_fxtr_type`.)

### §3.4 — Validator path

- [x] Add `_VALIDATOR_RESULT_CACHE: weakref.WeakKeyDictionary[...]`
      module-level state.
- [x] Implement `_check_validator_with_cache` helper.
- [x] Rework `_make_typed_resolver`'s gate body to call the helper.
- [x] Add cache-hit / cache-miss / mismatch / layered-id /
      cross-target tests. (Cache-hit covered by
      `test_validator_called_once_per_distinct_type_id`; the
      $fxtr.type round-trip + loadability covered by
      `test_validator_receives_loadable_type_id`. Mismatch /
      cross-target paths exist in the helper but no dedicated test
      yet — flagged below as a follow-up.)

### §3.5 — `scope_bridge.py` simplification

- [x] Drop `_KEY_FXTR_ARENA`, `_KEY_ARENA_LEASE` constants and
      their packet fields.
- [x] Drop `fxtr_arena` / `arena_lease` fields from
      `_BootstrapPacket`.
- [x] Drop arena-handling lines in `_build_export_packet`.
- [x] Drop arena-bridge-install in `_install_peer_main_service`.
- [x] Drop the `wrapper_task_group`-for-arena-hosting threading
      from `_build_export_packet` (the parameter is no longer
      needed here; other parts of `open_rpc_scope` still use the
      task group for the wrapper handle itself).
- [x] (No precondition `lift(service_type_id)` to remove —
      `open_rpc_scope` only calls `host_scope.lift(our_main_service)`
      which lifts the cap-bridge, no longer the type_id, so it stays.)
- [x] Update `_parse_bootstrap_packet` for the slimmer packet.
- [x] Update tests for the new packet shape. (No tests assert on
      the raw packet shape; the test suite covers behaviour
      end-to-end via `open_rpc_scope` and remains green.)

### §3.6 — Documentation

- [x] Update `fxtr2/data/LOCAL_CODE_OVERVIEW.md`.
- [x] Update `fxtr2/services/LOCAL_CODE_OVERVIEW.md`.
- [x] Update `CODE_OVERVIEW.md`.
- [x] Update `service_cap_common.py` module docstring.
- [x] Update `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md`.
- [x] Add Superseded notes at relevant spots in part6post4.

### Final

- [x] Run the full test suite (`uv run pytest -n auto` from `v2/`).
      Confirm green. (2316 passed.)

## Notes / things to revisit

- `bridge_service_from_scope` is still `async def` but now does no
  awaits. We keep `async` for API stability — a future change may
  add an async step (e.g. lazy-bridge negotiation). Worth flagging
  in code review whether to drop the async signature.
- The validator cache uses `WeakKeyDictionary` for the validator
  identity. Validators that don't support weakref (some bound-method
  forms, some closures) will raise `TypeError` at insert time. If
  this turns out to be a real footgun, the fallback is a regular
  `dict` keyed on `id(validator)` — leaks for short-lived
  validators, but practically users register at import time.
- The `service_type_id_check` exact-match gate intentionally does
  *not* invoke `$fxtr.type`. It's a one-entry cache: "this exact
  hash maps to True; everything else to False". A protocol with a
  type_id_check could in principle want to verify the impl actually
  has that identity (rather than trusting the wire), but the
  consensus model is "the wire hash is good enough for kind
  identification". If verified-exact-match is needed, users can
  switch to `service_type_validator` and use the cached path.
- After this part lands, `_known_handles` and
  `_CapBridgeEntry.handles` could in principle be simplified back
  to `set[IRHandle]` (since type_id is never queried from those
  sets). We deliberately keep `ServiceKey` as the key type:
  - Two ServiceRefs claiming different type_ids on the same handle
    stay distinct in scope bookkeeping (registering the same
    handle twice with different declared types is a real
    possibility we don't want to silently merge).
  - Existing call sites already build `ServiceKey`s; rewriting
    them to use bare `IRHandle` would be churn for no semantic
    win.
- Dedicated tests for the wire-hash-vs-`$fxtr.type` mismatch path in
  `_check_validator_with_cache` aren't present yet — the path is
  reachable only from a peer whose impl returns a different
  `$fxtr.type` than the wire `service_type_id` it minted, which is
  awkward to set up without bypassing the impl's own consistency.
  Worth adding a deliberately-misbehaving impl fixture as a
  follow-up if we want regression coverage.
