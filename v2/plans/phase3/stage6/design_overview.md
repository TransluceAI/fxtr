# Stage 6: Fxtr Entity Bridge for Cap-IR

## Goal

Bridge fxtr's entity/scope system with Cap-IR's capability-passing RPC, allowing two fxtr2
programs to exchange fxtr entities over an arbitrary transport. The bridge is a layer on top of
Cap-IR that provides fxtr-aware wrappers (`FxtrRPCHandle`, `FxtrRPCPending`) with scope-aware
entity marshalling, so that users can pass `FxtrID`s and `EntityRef`s in RPC calls without
manually managing arena capabilities or data transfer.

## Key Design Principle: RPC Calls as Scope Boundaries

RPC calls are natural scope boundaries for entity data:
- They have clear ownership (caller dispatches, callee handles)
- They execute in a defined block with a known lifetime
- They encapsulate a set of entities being sent/received
- Any further entity access requires new RPC calls (with their own scopes)

This means the fxtr bridge does not need connection-level entity state or per-entity wrappers.
Instead, each call carries its own scope for the entities flowing through it.

## Value Types

Two type aliases define what can flow through the fxtr RPC layer:

```python
# For the top-level stream — no bare FxtrIDs (they have no scope to live in)
type FxtrRPCValue = (
    int | float | str | bool | bytes | None
    | list[FxtrRPCValue] | dict[str, FxtrRPCValue]
    | Exception | RPCTagged[Any, FxtrRPCValue]
    | FxtrRPCHandle | FxtrRPCPending
)

# For inside calls — same as above plus FxtrID
type FxtrRPCCallValue = FxtrID | FxtrRPCValue  # (with recursive substitution)
```

The type system enforces the invariant: bare FxtrIDs only appear inside call arguments/results
(where a per-call scope manages their lifetime), never at the top-level stream.

## User-Facing Types

### `FxtrRPCHandle`

A wrapper around `RPCHandle` with the same ownership and lifetime rules, but with fxtr scope
awareness at dispatch time. There is one concrete implementation — the difference between
local and remote handles is how the underlying Cap-IR handle was created, not the wrapper type.

```python
class FxtrRPCHandle:
    def dispatch(
        self,
        attr_path: Sequence[str | int],
        request: FxtrCall | FxtrGetAttribute,
        scope: FxtrScope | None = None,  # defaults to active scope
    ) -> FxtrRPCPending: ...

    def dispatch_and_wait(
        self,
        attr_path: Sequence[str | int],
        request: FxtrCall | FxtrGetAttribute,
        scope: FxtrScope | None = None,
    ) -> ...: ...

    # clone, discard, wait_until_teardown same as RPCHandle
```

The `scope` parameter specifies:
1. **Source of argument entities**: FxtrIDs in the `FxtrCall.arg` must be loadable from this scope.
2. **Lifetime anchor for the result**: The returned `FxtrRPCPending` is bound to this scope.

For `FxtrGetAttribute` (which has no arguments), the scope parameter is not required on the
caller side; the result's lifetime is tied only to the RPC connection scope.

### `FxtrRPCPending`

A wrapper around `RPCPending` bound to both the underlying `RPCPending` and the `FxtrScope`
where the call was dispatched. The pending is considered torn down when either the RPC
connection or the dispatch scope goes down.

```python
class FxtrRPCPending[T]:
    async def pull(
        self,
        into_scope: FxtrScope | None = None,  # defaults to active scope
        copy_into_scope: bool = False,
    ) -> T: ...

    def as_handle(self) -> FxtrRPCHandle: ...

    # clone, discard, wait_until_teardown same as RPCPending
```

#### Pull semantics

The `pull` method retrieves the result and tracks any FxtrIDs in the result in the destination
scope. The two modes mirror `FxtrNamespace.retrieve()`:

**`pull(into_scope=t)` (default, no copy)**:
- Requires `t` is a descendant of **both** the dispatch scope **and** the RPC connection scope.
- FxtrIDs in the result are lazily mounted into `t`. Loading them may trigger on-demand fetching
  from the remote side via the underlying arena capability.
- This is safe because the scope hierarchy structurally guarantees both the local backing data
  (dispatch scope) and the remote connection (RPC connection scope) outlive `t`.

**`pull(into_scope=t, copy_into_scope=True)`**:
- Only requires that the dispatch scope and RPC connection are alive at pull time.
- `t` can be any scope (does not need to be a descendant of either).
- Entity data is eagerly copied into `t`. After the copy, the entities are fully local and
  independent of the RPC connection.

The `into_scope` defaults to the active scope if not specified.

#### Teardown

A `FxtrRPCPending` tears down when:
- The underlying `RPCPending` tears down (connection broken, discarded, rejected), OR
- The dispatch scope exits

This matches fxtr's convention that entity IDs do not keep scopes alive — the scope's lifetime
is managed by the user, and the pending becomes unusable when the scope exits.

#### Pipelining

`FxtrRPCPending` supports pipelining via `dispatch()` (inherited from the underlying
`RPCPending`). Pipelined calls also take a scope parameter. When chaining calls A → B:
- If both dispatched from the same scope, things work naturally — B's callee scope can access
  entities from A's result because the arena capabilities flow through at the Cap-IR level.
- The scope constraint is that B's dispatch scope must be able to load entities from A's
  dispatch scope (meaning that B's scope must be a descendant of A's, which holds trivially if
  they're the same scope).

### Contexts: `LocalFxtrRPCContext` and `RemoteFxtrRPCContext`

There are two context types, paralleling the Cap-IR level (`LocalRPCContext` vs transport
contexts):

#### `LocalFxtrRPCContext`

For in-process use. Wraps Python objects as `FxtrRPCHandle`s with full scope management,
without any stream or transport.

```python
class LocalFxtrRPCContext:
    scope: FxtrScope  # connection-level scope

    def wrap(self, obj) -> FxtrRPCHandle:
        """Wrap a Python object as an FxtrRPCHandle."""
        ...
```

Internally owns a `LocalRPCContext` for creating Cap-IR handles. When calls arrive on wrapped
handles, the context intercepts them to set up per-call scopes, decode arguments, and encode
results — all transparently. Useful for testing and for in-process entity exchange.

#### `RemoteFxtrRPCContext`

Lifts a Cap-IR `ObjectStream[IRValue[JSONLeaf]]` into an `FxtrRPCValue` stream.

```python
class RemoteFxtrRPCContext:
    scope: FxtrScope  # connection-level scope

    async def send(self, value: FxtrRPCValue) -> None: ...
    async def receive(self) -> FxtrRPCValue: ...
```

**Transport-independent**: takes a Cap-IR ObjectStream, not a transport. Works with any
transport (JSON, CBOR, in-memory). The transport lifetime is managed separately.

Also internally owns a `LocalRPCContext` for creating arena capability handles for outgoing
entity data.

The connection scope is a child of whatever scope the context is entered in. It lives as long
as the context. User code that wants lazy pull (without `copy_into_scope`) should run in a
child scope of this connection scope.

#### Abort on unexpected teardown

Both context types monitor for unexpected teardown. If the underlying transport dies (for
`RemoteFxtrRPCContext`) or the context exits, all handles and pendings tear down:
- The connection scope refuses further operations
- All outstanding `FxtrRPCPending`s are torn down
- Any lazy-mounted arena capabilities become broken
- New dispatches raise immediately

When the context exits, it forcibly closes all per-call scopes it holds, ensuring cleanup is
bounded by the context manager lifetime.

#### Typical usage

```python
async with FxtrScope() as outer:
    # Set up transport (any transport works)
    async with json_transport_stream(json_stream) as cap_ir_stream:
        # Lift into fxtr-aware layer
        async with RemoteFxtrRPCContext(cap_ir_stream) as rpc:
            # rpc.scope is a child of outer
            async with FxtrScope() as working:
                # Receive a value — may contain FxtrRPCHandles
                value = await rpc.receive()
                handle = extract_handle(value)

                # Dispatch a call with entities from working scope
                # (note: passing `working` is not required, as this is the default)
                pending = handle.dispatch([], FxtrCall(arg_with_ids), scope=working)

                # Pull result — working descends from both dispatch scope and rpc.scope
                # (note: passing `working` is not required, as this is the default)
                result = await pending.pull(into_scope=working)

                # Or copy into a scope that outlives the connection
                result = await pending.pull(into_scope=outer, copy_into_scope=True)
```

For in-process use (e.g., testing):

```python
async with LocalFxtrRPCContext() as ctx:
    handle = ctx.wrap(my_service_object)
    async with FxtrScope() as working:
        pending = handle.dispatch([], FxtrCall(arg_with_ids), scope=working)
        result = await pending.pull(into_scope=working)
```

## Callee Side

On the remote side, each incoming call runs in a fresh per-call scope:

```python
# Framework does this internally for each incoming call (in a background task):
async with FxtrScope(include=[rpc_context_scope]) as call_scope:
    # Argument FxtrIDs are loadable from call_scope, backed by
    # arena capabilities pointing to the caller's dispatch scope.
    result = await user_handler(decoded_arg)
    # Result FxtrIDs stored in call_scope are captured. A return arena
    # capability is created from call_scope and sent back to the caller.
    # The scope must stay open because exiting it would take down its
    # arenas, making the return arena capability unusable.
    await wait_for_either(discard_notification, context_teardown)
    # Scope exits when caller discards the pending or context tears down.
```

The user handler does not need to know it's inside an RPC call. It just works with FxtrIDs and
the active scope normally. It can:
- Load argument entities from the active scope (which includes the call scope)
- Store new entities into the active scope
- Return FxtrIDs in the result, which the framework captures

The per-call scope on the remote side is kept alive (as a child of the RPC connection scope)
until the caller discards the corresponding `FxtrRPCPending`, at which point it can be cleaned
up.

## Architecture: Layered Stream Model

The system is structured as three layers, each lifting a bidirectional stream into a
higher-level abstraction:

```
Layer 1: Raw transport stream
  JSON ObjectStream (bidirectional JSON messages)
    │
    ▼  [Cap-IR transport: JSONTransportContext or SimpleJSONTransportContext]
    │
Layer 2: Cap-IR ObjectStream
  Bidirectional IRValue[JSONLeaf] with RPCHandles/RPCPendings
    │
    ▼  [Fxtr bridge: FxtrRPCContext]
    │
Layer 3: FxtrRPC ObjectStream
  Bidirectional IRValue[FxtrLeaf] with FxtrRPCHandles, scope-managed FxtrIDs
```

### ObjectStream abstraction

An ObjectStream is a bidirectional channel for sending and receiving typed values. At the
Cap-IR level, this can be implemented as a thin main-interface wrapper: each side exposes a
root capability with a `receive(value)` method, and sending is calling `receive` on the remote
side's root capability.

This is lightweight to implement on top of the existing transport infrastructure — the main
interface is just a single-method object.

### Layer independence

Each layer is independent of the layers below it:
- **Cap-IR transport** takes a raw JSON stream and produces a Cap-IR ObjectStream. It handles
  import/export tables, message encoding, and the wire protocol.
- **FxtrRPCContext** takes a Cap-IR ObjectStream and produces an FxtrRPC ObjectStream. It handles
  scope management, arena capabilities, and FxtrID marshalling. It does not know what transport
  is being used.
- For **testing**, local in-memory channels can be used at any layer — the FxtrRPC layer can be
  tested with local caps (no network), and the Cap-IR ObjectStream can be tested with in-memory
  streams.

### Entry points

Values received from the FxtrRPC ObjectStream may contain `FxtrRPCHandle`s. These handles are
the entry points for scope-managed entity exchange — the stream gives you capabilities, and you
dispatch on them to exchange entities with per-call scope management.

At the top level of the stream, there are no FxtrIDs (since there's no call scope for them to
live in). FxtrIDs only appear inside calls dispatched on `FxtrRPCHandle`s, where the per-call
scope machinery manages their lifetime.

## Cap-IR Level Mechanism

The fxtr bridge is implemented as a translation layer between `FxtrRPCHandle`/`FxtrRPCPending`
and the underlying `RPCHandle`/`RPCPending`. This layer is internal and not exposed to users.

### IRValue conversion

The bridge converts between an extended leaf type (`FxtrLeaf = FxtrID | JSONLeaf | bytes`)
and the transport's leaf type (`JSONLeaf`) by walking IRValue trees and applying tag-based
encoding/decoding:

- **FxtrIDs** become `RPCTagged("fxtr:id", <id_bytes>)` in the transport IRValue
- **bytes** become `RPCTagged("fxtr:bytes", <base64_string>)` on JSON transports (or native
  bytes on CBOR transports)

### Per-call arena capabilities

When dispatching a call with FxtrIDs in the argument:

1. Collect all FxtrIDs in the argument tree
2. Create a per-call arena containing these entities (forked from the dispatch scope's arena)
3. Wrap the arena as an `RPCHandle` implementing the arena loading interface
4. Include this arena handle in the encoded call (as a tagged capability alongside the argument)
5. On the receiving side, detect the arena handle and create a scope backed by it
6. FxtrIDs in the argument are reconstructed and loadable from this scope

For return values, the same mechanism operates in reverse: the callee's call scope is wrapped
as an arena capability and sent back to the caller.

### Arena capability interface

The arena capability exposes a single method, mirroring the HTTP `/data/` endpoint pattern
from `experimental/http/`:

- `load_data(id_bytes)` → returns entity CBOR bytes (or None for derived nodes), precomp
  relationships, and dependency list

This is intentionally minimal. The `RemoteArenaAdapter` on the receiving side follows the
same pattern as `HttpReadableArena`: it calls `load_data`, ingests precomp, tracks child IDs,
and exposes the standard `ReadableArena` interface. Bulk loading can be added later if
round-trip latency becomes an issue.

### Forwarding optimization

When the caller sends entities and the callee returns some of them (forwarding), the caller's
pull implementation can check: "is this FxtrID already in my dispatch scope?" If yes, it skips
the remote fetch entirely. This avoids unnecessary round-trips for forwarded entities.

### Prefetching (future optimization)

The arena capability model supports transparent prefetching without API changes:
- The sender can speculatively include flat data for near dependencies alongside the arena
  capability
- The receiver's arena checks a local cache first, falling back to the capability on miss
- This optimization can be added later without changing the user-facing `FxtrRPCHandle` or
  `FxtrRPCPending` APIs

## Handles vs Entity Data

`FxtrRPCHandle` itself does not carry a scope binding. Handles are capabilities (identity-bearing,
independently managed) while FxtrIDs are data (scope-managed). They coexist naturally in the
same IRValue tree — a return value can contain both FxtrIDs (tracked in a scope) and handles
(managed by their own lifetime rules).

If the callee wants to expose a stateful object alongside entity data, it can return both a
handle and FxtrIDs in the same result. The handle's lifetime is independent of the return
scope.

## Scope Hierarchy Summary

```
Global scope
  └─ RPC connection scope (lives as long as the connection)
       └─ User working scope (typical location for dispatches)
            └─ Per-pull child scopes (for lazy-mounted results)

Remote side:
  RPC connection scope
    └─ Per-call scope (lives until caller discards pending)
         └─ Handler's working scopes
```

## Invariants

1. **Closed subgraph**: When `pull` is called without `copy_into_scope`, the destination scope
   descends from the RPC connection scope, ensuring that lazy-loaded entities can always be
   fetched while the scope is alive.

2. **Scope exit = teardown**: A `FxtrRPCPending` whose dispatch scope has exited is torn down.
   You cannot pull from it. This prevents dangling references to entity data whose backing
   storage has been released.

3. **Copy for independence**: `pull(copy_into_scope=True)` eagerly copies entity data, making
   it independent of both the dispatch scope and the RPC connection. Use this when the result
   must outlive the call or connection.

4. **Per-call isolation**: Each call on the remote side gets its own scope. The handler cannot
   access entities from other calls except through explicit RPC calls, which create their own
   scopes.
