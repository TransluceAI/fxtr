# Substage 7 — `FxtrScope` ↔ RPC bridge

For background see [overview.md §Substage 7](overview.md#substage-7--fxtrscope--rpc-bridge)
and [sketch.md §6](sketch.md). Substage 7 wraps `LeasedCapRPCConnection`
with a thin scope-aware layer that exchanges main-service references
between two peers and yields a fresh `FxtrScope` with the peer's main
service mounted in it.

Bundled into the same substage (but **conceptually independent and
separately testable**): an enhancement to `IRException` traceback
propagation across RPC. Right now we throw away the traceback at
every wrap site and serialization step; this substage adds a
`traceback_info: tuple[str, ...]` field that accumulates
formatted-traceback strings hop by hop, so a Python exception thrown
in a remote call body surfaces with its original stack on the caller
side, even after a JSON conversion layer.

The two pieces are landed independently — the IRException work
ships first as a standalone change, exercised via unit + codec
tests; `open_rpc_scope` then layers on top.

---

## 1. `open_rpc_scope` — design

### 1.1 Public surface

```
@dataclass(frozen=True)
class RPCSession[T2]:
    scope: FxtrScope
    remote_service: ServiceRef[T2]


@asynccontextmanager
async def open_rpc_scope[T2](
    transport: anyio.abc.ObjectStream[RPCMessage],
    our_main_service: ServiceRef[Any],
    *,
    remote_service_type: ServiceTypeAnnotation[T2] = UnknownService,
    name: str = "rpc_scope",
) -> AsyncGenerator[RPCSession[T2], None]: ...
```

- The caller has already mounted (or bridged) `our_main_service` in
  the currently-active scope. `open_rpc_scope` does **not** mount
  anything itself; it just refers to the existing ref.
- `remote_service_type` is the target type the yielded `ServiceRef` will
  carry. Defaults to `UnknownService`. Substage 8 supplies typed
  `ServiceTypeAnnotation`s here without changing this layer.
- `name` is propagated to the wrapper lifetime, the conn's logging
  context, and the inner `FxtrScope`.

### 1.2 Lifetime nesting

```
host_scope = FxtrScope.get_active()           ─ outer (caller-owned)
  wrapper_lt + wrapper_tg                     ─ open_lifetime_task_group
    LeasedCapRPCConnection                    ─ async CM
      user_scope = FxtrScope(mountable=True,  ─ async CM
                             include=[host_scope],
                             name=name)
        yield RPCSession(scope=user_scope,
                         remote_service=their_ref)
```

Drain on exit (innermost first): `user_scope.aclose` →
`conn.__aexit__` (drains then closes the transport) → `wrapper_lt`
exits its CM. `host_scope` keeps its outer lifetime.

The structural reason for this nesting: the **scope is a child of
the connection** so that user code mounting things into `user_scope`
during the body keeps the connection alive until the user is done.
The wrapper sits *outside* the connection because the wrapper is
what the connection uses as its main interface — it has to exist
before the conn opens. The wrapper does not host any user-visible
service, just the bootstrap method.

### 1.3 Bootstrap protocol

After both connections handshake, each side sends its main-service
information to the peer **as a single call argument**. The peer's
wrapper handle receives that call, shovels the parsed packet onto
an in-process `anyio` memory channel, and returns `None`. The
`open_rpc_scope` body reads the channel to get the peer's packet,
opens the user scope, and installs the peer's main service into it.

This is the "push our main service info immediately" shape, not a
"call us back to ask for it" shape — sending eagerly removes a
round-trip and means each side gets the peer's info as soon as the
peer has finished sending.

Wire shape of the packet:

```
IRTagged("fxtr_main_export", {
    "handle":       IRHandle,                       # peer's real main service
    "type_id":      IRTagged(FXTR_ID_TAG, bytes),   # service_type_id
    "fxtr_arena":   IRHandle,                       # arena containing type_id
    "handle_lease": Lease[handle.token],
    "arena_lease":  Lease[arena.token],
})
```

This is `IRCallArg`-shaped: the Cap-IR devaluator already knows how
to serialize handles, leases, tagged values, and bytes. No new wire
machinery, no protocol bump.

The call form is `Call("fxtr_export_main_service", <packet>)` —
chosen as a method name so the wrapper's request validator has a
single recognized method.

### 1.4 `_MainServiceWrapperHandle`

Internal-only. A tiny `IRHandleLocalImplBase[Any]` whose only role
is to receive the inbound bootstrap packet and forward it to the
`open_rpc_scope` body via a memory channel.

```
class _MainServiceWrapperHandle(IRHandleLocalImplBase[Any]):
    _token: LifetimeToken[Any]
    _task_group: anyio.abc.TaskGroup
    _packet_send: anyio.streams.memory.MemoryObjectSendStream[_BootstrapPacket]

    async def _run_request_async(self, lease, request):
        # 1. Validate: Call("fxtr_export_main_service", IRTagged("fxtr_main_export", dict))
        # 2. Parse / type-check / extract dict fields → _BootstrapPacket
        # 3. await self._packet_send.send(parsed)   ── ownership of leases transfers
        # 4. lease.discard(); return None
```

Validation failures raise `IRException(category="MalformedBootstrap")` —
Cap-IR's standard reject path emits a `RejectMessage`, the peer's
`request` raises, and the peer's `open_rpc_scope` unwinds.

`_BootstrapPacket` is a simple frozen dataclass internal to
`scope_bridge.py`:

```
@dataclass(frozen=True)
class _BootstrapPacket:
    handle:       IRHandle[Any]
    type_id:      FxtrID
    fxtr_arena:   IRHandle[Any]
    handle_lease: Lease[Any]   # subject == handle.token
    arena_lease:  Lease[Any]   # subject == fxtr_arena.token
```

**Lease ownership.** When the wrapper writes the parsed packet to
the channel, the leases inside transfer to the recv side. The body
is responsible for either (a) consuming both leases successfully via
`_install_peer_main_service`, or (b) on any failure between recv
and successful install, discarding both leases via
`discard_leases_in` (which handles the dataclass-of-leases tree
shape). See §1.7 for the install-side try/except.

The wrapper's task group / ULL come from a single
`open_lifetime_task_group(f"{name}/wrapper")` enclosing the conn. The
wrapper outlives the conn (the connection's main-grant export holds
a clone of the wrapper's root lease; the lease is only released after
the conn fully drains).

**Channel teardown.** The packet send/recv streams are entered as
`async with` resource context managers in the body. Once the body
has received the peer's packet and proceeds, both ends close on
exit. A late wrapper invocation hitting a closed channel just fails
in the wrapper's `_run_request_async` — peer surfaces that as an
IRException, which is fine: in practice we won't have exited the
body before recv, so the only path here is an exception unwinding
the body, in which case the conn is already aborting anyway.

### 1.5 Building the outbound packet

Helper inside `scope_bridge.py`:

```
def _build_export_packet(
    *, host_scope: FxtrScope,
    our_main_service: ServiceRef[Any],
    wrapper_task_group: anyio.abc.TaskGroup,
) -> IRTagged[Any, Any]:
    """Build the IRTagged packet."""
    # 1. Lift our_main_service's bridge into host_scope (eager — host_scope
    #    is going to need a local entry anyway for any subsequent operation).
    #    Then clone the lease via the same internal path
    #    ServiceProxyContext.mint_lease_for_key uses. Inline the necessary
    #    bits — no synthetic ServiceProxyContext, no new public helper.
    #    Concrete or pipelined; either form rides through Cap-IR
    #    (the devaluator emits AwaitFutureExpr for the pipelined form).
    # 2. Fork a sub-arena from host_scope._main_arena containing
    #    service_type_id (single ID); wrap with ArenaIRHandleAdapter on
    #    wrapper_task_group. The fork shares host_scope.main_token.
    # 3. arena_lease = host_scope._main_lease.clone() — same token as the fork.
    # 4. type_id encoded as IRTagged(FXTR_ID_TAG, bytes(service_type_id)).
    # 5. Return IRTagged("fxtr_main_export", dict).
```

Notes:
- Hosting the `ArenaIRHandleAdapter` on `wrapper_task_group` (not
  host_scope's) decouples it from host_scope's drain. host_scope is
  outer to the wrapper and stays alive throughout, so this is safe.
- The lease-minting logic is inlined — looking up the bridge in
  host_scope's cap-bridge graph, lifting it to a local entry if
  needed, and cloning. We don't need to expose a new public-internal
  scope helper; the existing internal `_lift_bridge_for_service_key`
  + bridge-clone primitives are enough. If implementation surfaces
  duplication with `ServiceProxyContext.mint_lease_for_key`, factor
  out at that point.

### 1.6 Sequence inside `open_rpc_scope`

Concretely:

```
host_scope = FxtrScope.get_active()
# Eager precondition: lift our_main_service into host_scope.
# This lifts as a side effect (fine — host_scope was going to need
# a local entry anyway). Raises ValueError if unreachable.
host_scope._lift_bridge_for_service_key(
    ServiceKey(our_main_service.handle, our_main_service.service_type_id)
)

async with open_lifetime_task_group(f"{name}/wrapper") as (wrapper_lt, wrapper_tg):
    packet_send, packet_recv = anyio.create_memory_object_stream[_BootstrapPacket](1)
    async with packet_send, packet_recv:
        wrapper = _MainServiceWrapperHandle(
            token=wrapper_lt.token,
            task_group=wrapper_tg,
            packet_send_stream=packet_send,
        )

        async with LeasedCapRPCConnection(
            transport, wrapper, wrapper_lt.root_lease.take()
        ) as conn:
            our_packet = _build_export_packet(
                host_scope=host_scope,
                our_main_service=our_main_service,
                wrapper_task_group=wrapper_tg,
            )

            peer_packet: _BootstrapPacket | None = None

            async def do_send():
                await conn.their_main_interface.request(
                    conn.their_main_lease.take(),
                    Call("fxtr_export_main_service", our_packet),
                )

            async def do_recv():
                nonlocal peer_packet
                peer_packet = await packet_recv.receive()

            async with anyio.create_task_group() as bootstrap_tg:
                bootstrap_tg.start_soon(do_send)
                bootstrap_tg.start_soon(do_recv)
            assert peer_packet is not None

            try:
                async with FxtrScope(
                    mountable=True, include=[host_scope], name=name
                ) as user_scope:
                    their_ref = _install_peer_main_service(
                        user_scope, peer_packet, remote_service_type
                    )
                    # Past this point, peer_packet's leases have been
                    # consumed by the install — cleanup-on-raise below
                    # is a no-op.
                    peer_packet = None
                    yield RPCSession(scope=user_scope, remote_service=their_ref)
            finally:
                if peer_packet is not None:
                    discard_leases_in(peer_packet)
```

Notes:

- **Eager precondition** is the lift call itself; we don't need a
  separate side-effect-free lookup. If the lift can't find a bridge
  for the key, it raises and `open_rpc_scope` is still in its
  pre-conn state — nothing to clean up.
- **`take()` on `their_main_lease`.** After the bootstrap exchange
  we don't care about the peer's wrapper anymore — taking the lease
  consumes it directly. The conn's own lifecycle handles drain.
- **Concurrent send + recv.** Either order works; the channel is
  buffer-1 so peer's send can land before our recv is registered.
  Wrapping in a tiny task group keeps the cleanup path simple
  (cancellation of one branch propagates).
- **Cancellation.** If the caller cancels mid-bootstrap, the
  bootstrap task group propagates; both `do_send` and `do_recv`
  unwind; the conn aborts via its standard exception path.
  `peer_packet` may or may not have been set; the outer `try/finally`
  discards if so.
- **Failure between recv and successful install.** If
  `_install_peer_main_service` raises (or anything else does between
  recv and the `peer_packet = None` line), the `finally` block
  discards the packet's leases via `discard_leases_in`.
- **No timeout knob.** Bounded waits are the caller's responsibility
  via an outer cancel scope.

### 1.7 `_install_peer_main_service`

```
def _install_peer_main_service(
    user_scope: FxtrScope,
    packet: _BootstrapPacket,
    remote_service_type: ServiceTypeAnnotation[T2],
) -> ServiceRef[T2]:
    """Install the peer's main service into user_scope. Specialised to
    one handle + one arena + one type_id; both leases ship as
    Independent (already-live, not derived from anything in user_scope).

    Mirrors a single iteration of service_cap_common._install_returned_resources
    but trimmed to the bootstrap shape — no chain converter, no
    multi-token de-dup, no fxtr_call/fxtr_return packet decode.
    """
    # 1. Bridge the peer's arena into user_scope's main arena so that
    #    type_id becomes is_tracked. The arena is foreign — wrap with
    #    CapIRArena.direct(token=arena_lease.subject, remote=fxtr_arena,
    #    initial_tracked=[type_id]) and lazily_attach_from_arena under
    #    an IndependentLocalBridge holding arena_lease.
    cap_ir = CapIRArena.direct(
        token=packet.arena_lease.subject,
        remote=packet.fxtr_arena,
        initial_tracked=[packet.type_id],
    )
    try:
        user_scope._main_arena.lazily_attach_from_arena(
            user_scope._main_lease.clone(),
            [packet.type_id],
            cap_ir,
            IndependentLocalBridge(lease=packet.arena_lease),
        )
    finally:
        cap_ir.close()  # store keeps the sibling fork; we close the original

    # 2. Install the handle bridge. handle_lease is Independent
    #    (peer-owned token, not derived from anything in user_scope) →
    #    IndependentLocalBridge.
    return user_scope.attach_bridged_lowlevel_cap(
        handle=packet.handle,
        target_type=remote_service_type,
        service_type_id=packet.type_id,
        bridge=IndependentLocalBridge(lease=packet.handle_lease),
    )
```

Lease ownership: `packet.arena_lease` is consumed by
`lazily_attach_from_arena` (via the bridge). `packet.handle_lease`
is consumed by `attach_bridged_lowlevel_cap`. The packet's other
fields (`handle`, `fxtr_arena`, `type_id`) carry no leases.

### 1.8 Errors and edge cases

- **Wrapper called with the wrong method name.** The validator
  raises `IRException(category="UnknownMethod")`; peer's bootstrap
  call raises; their `open_rpc_scope` unwinds.
- **Lifetime mismatch on inbound leases.** If the peer's
  `handle_lease.subject` doesn't match `handle.token`, the wrapper
  raises `IRException(category="MalformedBootstrap")`.
- **Cancel during yield body.** User exits via exception; the inner
  `async with FxtrScope` triggers `force_close`; the connection's
  `__aexit__` aborts (sending `AbortMessage`); the wrapper lifetime
  exits via the same exception.
- **Late or duplicate wrapper invocation.** Once the body has read
  the channel and proceeded, the streams' `async with` exits and
  the channel closes. A subsequent peer dispatch lands on a closed
  send stream and the wrapper fails with whatever error
  `MemoryObjectSendStream` raises on a closed stream. In practice
  this can't happen on a healthy bootstrap (we don't exit the body
  until *after* recv succeeds, by which time peer has sent), so we
  don't try to specify the exact failure surface.

---

## 2. `IRException.traceback_info`

### 2.1 Field

```python
class IRException[D](Exception):
    def __init__(
        self,
        message: str,
        category: str | None = None,
        data: D | None = None,
        traceback_info: tuple[str, ...] = (),
    ):
        super().__init__(message)
        self.message = message
        self.category = category
        self.data = data
        self.traceback_info = traceback_info
```

Stored as a `tuple[str, ...]` (immutable; one element per "hop"
through serialization or initial wrap). Each element is a complete
formatted traceback as produced by
`traceback.format_exception(type(exc), exc, exc.__traceback__)`.

### 2.2 `__str__` extension

```python
def __str__(self) -> str:
    base = f"[{self.category}] {self.message}" if self.category else self.message
    if not self.traceback_info:
        return base
    return base + "\n\n" + "\n\n".join(self.traceback_info)
```

Single blank line between adjacent entries; one blank line between
the base header and the first entry.

### 2.3 `wrap_exception` helper

In `fxtr2/leases/leased_cap_common.py`, alongside `discard_leases_in`:

```python
def wrap_exception(
    exc: BaseException,
    *,
    category: str | None = None,
) -> IRException[Any]:
    """Convert a non-IRException Python exception into an IRException,
    capturing its formatted traceback as the initial traceback_info entry.

    No-op if ``exc`` is already an IRException — it passes through
    unchanged. Encode-time enrichment (in ``json_serdes.encode_static``)
    captures any local Python __traceback__ / __notes__ on
    already-IRException values automatically, so wrap-side logic
    doesn't need to do anything for them.
    """
    if isinstance(exc, IRException):
        return exc
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return IRException(
        message=str(exc),
        category=category or type(exc).__name__,
        traceback_info=(formatted,),
    )
```

### 2.4 Wire codec

`json_serdes.encode_static(IRException)` — bumped wire shape:

```
["error", message, category, data_or_null, traceback_info_list]
```

5 elements, always. v1 is not yet released (per the existing wire
docs), so we extend the form unconditionally; the decoder requires
exactly 5 elements.

```python
# encode_static
if isinstance(value, IRException):
    data_enc = None if value.data is None else encode_static(value.data)
    formatted = _format_local_exc_state(value)
    new_traceback_info = list(value.traceback_info)
    if formatted is not None:
        new_traceback_info.append(formatted)
    return ["error", value.message, value.category, data_enc, new_traceback_info]


def _format_local_exc_state(exc: IRException) -> str | None:
    """Return formatted traceback for an IRException whose Python state has
    grown since the last encode (raised, caught, has notes attached). None
    if no local Python context to capture.
    """
    if exc.__traceback__ is None and not getattr(exc, "__notes__", None):
        return None
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
```

`decode_static` for `["error", ...]` extends to read the
5-element form and construct the IRException with
`traceback_info=tuple(traceback_info_list)`.
`MalformedStaticError` if the decoded list has anything other than
5 elements.

Wire-codec test updates: every existing test that encodes/decodes
an IRException needs to be updated to include the new slot
(typically empty list, since most tests construct IRException
without raising).

### 2.5 `copy_ir_value`

Existing reconstruction at `leased_cap_common.py:104`:

```python
new_exc = IRException(
    message=value.message,
    category=value.category,
    data=new_data,
    traceback_info=value.traceback_info,    # NEW: preserve through copy
)
```

The Python `__traceback__` / `__cause__` / `__context__` /
`__notes__` aliasing block stays as-is.

### 2.6 Migration of catch-and-wrap sites

Replace `IRException(message=str(exc), category=type(exc).__name__)`
with `wrap_exception(exc)` at each site. Identified call sites
(verify during implementation; grep
`IRException(message=str(exc)` plus targeted scan of the lowering
layer):

- `fxtr2/leases/leased_cap_common.py:1224` — runner in
  `spawn_ir_future_in_task_group`.
- `fxtr2/rpc/lowering_message_handlers.py:339, 539` — request
  handlers wrapping non-IR exceptions raised inside
  `_run_request_async`.
- `fxtr2/services/service_cap_common.py` — `_dispatch_body`'s
  catch (look up exact line during implementation).

Sites that construct `IRException(...)` *without* an underlying
caught Python exception (e.g.
`IRException("Protocol violation", category="ProtocolError")`) are
unchanged — there's no Python traceback to capture, and the
encode-time enrichment will pick up any `__traceback__` that
accumulates if the IRException ever gets raised.

---

## 3. File layout

| File | Adds / changes |
|---|---|
| `fxtr2/rpc/scope_bridge.py` (new) | `RPCSession`, `open_rpc_scope`, `_MainServiceWrapperHandle`, `_BootstrapPacket`, `_build_export_packet`, `_install_peer_main_service`, `_check_service_reachable` |
| `fxtr2/leases/leased_cap_ir.py` | `IRException.traceback_info` field + `__str__` |
| `fxtr2/leases/leased_cap_common.py` | `wrap_exception`; preserve `traceback_info` in `copy_ir_value`; migrate `spawn_ir_future_in_task_group` wrap site |
| `fxtr2/rpc/json_serdes.py` | 5-element error form on encode + decode; `_format_local_exc_state` |
| `fxtr2/rpc/lowering_message_handlers.py` | Migrate two wrap sites to `wrap_exception` |
| `fxtr2/services/service_cap_common.py` | Migrate `_dispatch_body` wrap site to `wrap_exception` |
| `tests/test_rpc/test_scope_bridge.py` (new) | Acceptance suite (§4.1) |
| `tests/test_leases/test_ir_exception_traceback.py` (new) | Unit tests for the field, `wrap_exception`, copy + JSON roundtrip (§4.2) |

`scope_bridge.py` does **not** import from `fxtr2/services/`. The
peer-side install is a specialized inline implementation rather than
reusing `_install_returned_resources` because the bootstrap shape is
much simpler (one handle, one arena, no chain converters) and we want
to keep the rpc layer dependency-light.

The bootstrap shape's bytes-tagged `FxtrID` constant (`FXTR_ID_TAG`)
is imported from `fxtr2/data/cap_ir_arena.py`, the canonical source
already used by services.

---

## 4. Tests

The two pieces of work are tested in independent suites — the
IRException enrichment is conceptually separate and gets validated
without going through the RPC bridge.

### 4.1 IRException enrichment

`tests/test_leases/test_ir_exception_traceback.py` (new):

- **Field default + `__str__`** — Empty default; non-empty stringifies
  with blank-line separators; category-prefixed header still applies.
- **`wrap_exception` semantics** — Plain Python exception → `IRException`
  with one-element `traceback_info`. Already-IRException → same object
  back, `traceback_info` unchanged.
- **`copy_ir_value` preserves the field** — Construct with
  `traceback_info=("a", "b")`, copy, assert preserved.

Codec tests added to `tests/test_rpc/test_json_serdes.py` (extending
existing IRException coverage):

- **JSON roundtrip preserves the field** —
  `decode_static(encode_static(exc)).traceback_info ==
  exc.traceback_info` for an IRException with no Python
  `__traceback__` (so `_format_local_exc_state` returns None and no
  new entry is appended).
- **`encode_static` adds entry on raised exception** — Construct,
  raise + catch, encode; the decoded value has one extra entry vs
  the pre-encode original.
- **Decoder rejects 4-element form** — Hand a `["error", m, c, d]`
  list to `decode_static`; expect `MalformedStaticError`.

Pre-existing wire-codec tests that hand-build or assert on the
encoded shape need updating to the 5-element form (scoped during
the implementation step, see §5 step 3).

### 4.2 RPC scope-bridge acceptance

`tests/test_rpc/test_scope_bridge.py` (new):

- **`test_basic_bootstrap`** — Two sides, each mounts an
  `UntypedServiceImpl` echo service in its own outer scope, then
  calls `open_rpc_scope`. After yield, each side resolves
  `session.remote_service` and invokes a method that calls back into
  the peer's mounted echo. Both sides exit cleanly. Uses raw
  `RPCMessage`-stream pair (matches the existing
  `test_lowering.py` setup convention).
- **`test_swap_services_via_method`** — The §6 sketch scenario as an
  *integration test*: substage-6 ServiceRef-as-arg /
  ServiceRef-as-return mechanics layered onto the RPC bridge. Side A
  mounts service X, sends `ServiceRef[X]` to side B's main service
  (whose method wraps + remounts X), receives a fresh
  `ServiceRef[X']`, invokes X'. Calls thread back into side A's X.
  All scopes drain. The new mechanic *is* "this all works through
  RPC"; per-mechanic unit coverage already lives in substage 6's
  tests.
- **`test_clean_drain`** — After both sides exit `open_rpc_scope`,
  the autouse leak fixture sees no leaked leases.
- **`test_eager_precondition`** — Calling `open_rpc_scope` with a
  ServiceRef whose handle isn't reachable from the active scope
  raises *before* opening the connection. (Specific exception type
  is whatever `_lift_bridge_for_service_key` raises today —
  `ValueError` per substage-6 convention.)
- **`test_cancel_during_bootstrap`** — Cancel one side immediately
  after entering its `open_rpc_scope`; the other side's CM raises
  promptly (the conn aborts the call).
- **`test_traceback_propagation`** — End-to-end: side A calls a
  method on side B's main service that raises a Python
  `ValueError`. Side A catches the resulting `IRException`;
  `traceback_info` is non-empty and contains side B's filename /
  line. This is the only test that exercises both pieces of
  substage-7 work simultaneously, but it's not load-bearing for the
  IRException enrichment — that's already validated by §4.1.

---

## 5. Implementation order

The two work streams are independent and can land in either order
(or in parallel). Suggested order: ship the IRException stream
first since it's smaller and unblocks the substage-7 traceback test.

### Stream 1 — IRException enrichment

1. **`IRException.traceback_info` field + `__str__`.** Smallest
   self-contained change; unit test the field default and string
   format. Existing tests stay green (default empty).
2. **`wrap_exception` helper + preserve in `copy_ir_value`.** Add
   helper to `leased_cap_common.py`; one-line update to the
   IRException reconstruction inside `copy_ir_value`. Unit tests
   for both. No call-site migration yet.
3. **JSON wire shape bump.** Update `encode_static` + `decode_static`
   for the 5-element form, plus `_format_local_exc_state`. Survey
   and update existing tests that hand-build `["error", ...]`
   literals (likely small — codec tests centralize this; substage
   6/5/4 tests use real exceptions).
4. **Migrate plain-exception wrap sites.** `spawn_ir_future_in_task_group`,
   the two sites in `lowering_message_handlers`, the
   `_dispatch_body` site in `service_cap_common`. Existing tests
   stay green; verify with the leak fixture and substage-6 service
   tests.
5. **Update docs.**
   - `fxtr2/leases/LOCAL_CODE_OVERVIEW.md` — `wrap_exception` next
     to `discard_leases_in`; `traceback_info` field on IRException.
   - `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` — wire-shape extension note.
   - `fxtr2/services/LOCAL_CODE_OVERVIEW.md` — `_dispatch_body`
     wrap-site change.

### Stream 2 — `open_rpc_scope`

6. **`scope_bridge.py` skeleton.** `RPCSession`,
   `_MainServiceWrapperHandle`, `_BootstrapPacket`,
   `_build_export_packet`, `_install_peer_main_service`, the
   `open_rpc_scope` CM. Get `test_basic_bootstrap` passing
   (raw-RPCMessage transport).
7. **Acceptance suite remainder.** swap_services test, drain test,
   cancel test, eager-precondition test, traceback-propagation
   test. The traceback test depends on Stream 1 having landed.
8. **Update docs.**
   - `CODE_OVERVIEW.md` — new `fxtr2/rpc/scope_bridge.py` entry;
     `RPCSession` + `open_rpc_scope` under the rpc/ section.
   - `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` — bootstrap-protocol
     section.

---

## 6. Open / followups

- **Bidirectional handshake protocol violations.** Today, a peer
  that never sends its bootstrap packet causes our `open_rpc_scope`
  to hang on `do_recv`. Fine for substage 7 (caller cancels via
  outer cancel scope). If real deployments need a synchronous
  "ready / not ready" signal, revisit.
- **Type-ID resolution for typed services (substage 8).** The
  current shape is agnostic to the meaning of `service_type_id`;
  for typed services it's whatever `_service_type_id(scope)`
  returns on the impl side — the peer just receives + tracks it.
  Substage 8 doesn't need to change `open_rpc_scope`.
- **Mountable host_scope.** `open_rpc_scope` works regardless of
  host_scope mountability — it doesn't mount anything on it. The
  `our_main_service` parameter is just a pre-existing ServiceRef
  whose handle is reachable from host_scope.
- **Multiple bootstrap calls.** If the peer somehow calls
  `fxtr_export_main_service` twice, the second send blocks on the
  size-1 channel. We currently raise `IRException` on the duplicate;
  could be relaxed to "silently accept the latest" if a real use
  case appears.
- **Wire codec extension for non-JSON transports.** The current
  Cap-IR in-memory transport doesn't go through JSON, so the
  `encode_static` enrichment doesn't apply. In-memory transports
  preserve the Python `__traceback__` directly via `copy_ir_value`.
  If a third transport is added (e.g. CBOR), it'll need to mirror
  the same enrichment hook.
- **`__notes__` representation.** `traceback.format_exception`
  already includes notes in its output, so the
  `_format_local_exc_state` implementation captures them
  automatically. If we ever want to surface notes separately (e.g.
  as a structured field), revisit.
