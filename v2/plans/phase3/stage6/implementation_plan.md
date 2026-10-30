# Stage 6: Fxtr Entity Bridge — Implementation Plan

See `design_overview.md` for the full design and `design_decision_log.md` for design rationale.

## Prerequisites

- **Stage 5 Part 4** (ObjectStream abstraction): `RemoteFxtrRPCContext` consumes an
  `ObjectStream[IRValue[JSONLeaf]]`. See `stage5_part4_object_stream.md`.
- **Stage 5 Part 5** (Discard notification): The fxtr bridge needs to know when all references
  to an arena capability handle have been discarded, to clean up per-call scopes. See
  `stage5_part5_discard_notification.md`.

## File Organization

New files in `fxtr2/rpc/`:

```
fxtr2/rpc/
├── fxtr_rpc_types.py     # FxtrRPCValue/FxtrRPCCallValue type aliases,
│                          #   FxtrRPCHandle, FxtrRPCPending (user-facing wrappers)
├── fxtr_rpc_local.py     # LocalFxtrRPCContext, _FxtrInternalCapIRRPCHandle,
│                          #   shared handle lifecycle base
├── fxtr_rpc_stream.py    # RemoteFxtrRPCContext: lift Cap-IR ObjectStream to FxtrRPC
├── fxtr_ir_conversion.py # Bidirectional IRValue[JSONLeaf] ↔ FxtrRPCCallValue conversion
└── fxtr_arena_cap.py     # ArenaCapabilityHandle (RPCHandle[JSONLeaf] wrapping arena)
│                          #   + RemoteArenaAdapter (RPCHandle → ReadableArena)
```

Tests in `tests/test_rpc/`:

```
tests/test_rpc/
├── test_fxtr_bridge.py       # Integration tests for the full bridge
└── test_fxtr_arena_cap.py    # Unit tests for arena capabilities
```

## Type System

### Value types

Two type aliases, paralleling how Cap-IR has `IRValue[Leaf]`:

```python
# For the top-level stream — no bare FxtrIDs (they have no scope to live in)
type FxtrRPCValue = (
    int | float | str | bool | bytes
    | None
    | list[FxtrRPCValue]
    | dict[str, FxtrRPCValue]
    | Exception
    | RPCTagged[Any, FxtrRPCValue]
    | FxtrRPCHandle
    | FxtrRPCPending
)

# For inside calls — same as above plus FxtrID
type FxtrRPCCallValue = (
    FxtrID
    | int | float | str | bool | bytes
    | None
    | list[FxtrRPCCallValue]
    | dict[str, FxtrRPCCallValue]
    | Exception
    | RPCTagged[Any, FxtrRPCCallValue]
    | FxtrRPCHandle
    | FxtrRPCPending
)
```

The type system enforces the invariant: bare FxtrIDs only appear inside call arguments/results,
never at the top-level stream.

### Request types

Parallel to Cap-IR's `Call[Leaf]` and `GetAttribute`:

```python
@dataclasses.dataclass(frozen=True)
class FxtrCall:
    arg: FxtrRPCCallValue

@dataclasses.dataclass(frozen=True)
class FxtrGetAttribute:
    pass
```

## Implementation Steps

### Step 1: Type definitions and IRValue conversion

**`fxtr_rpc_types.py`**: Define the type aliases (`FxtrRPCValue`, `FxtrRPCCallValue`),
`FxtrCall`, `FxtrGetAttribute`, and the `FxtrRPCHandle` / `FxtrRPCPending` classes.

`FxtrRPCHandle` and `FxtrRPCPending` each wrap a Cap-IR `RPCHandle[JSONLeaf]` /
`RPCPending[JSONLeaf, ...]` respectively. There is one concrete implementation of each — the
difference between local and remote is how the underlying Cap-IR handle was created, not the
wrapper type.

**`fxtr_ir_conversion.py`**: Bidirectional conversion that transforms between
`IRValue[JSONLeaf]` (with raw Cap-IR handles) and `FxtrRPCCallValue` (with `FxtrRPCHandle`s,
`FxtrRPCPending`s, `FxtrID`s, `bytes`) in a single pass.

**Encoding** (`encode_fxtr_call_value`): Walk an `FxtrRPCCallValue` tree → `IRValue[JSONLeaf]`:
- `FxtrID` → `RPCTagged("fxtr:id", <id_bytes_as_base64_string>)` using `_raw_data_id` only.
  Raises an error if the FxtrID has a `_provenance_parent` set, since provenance is a local
  concern and should not cross the wire.
- `bytes` → `RPCTagged("fxtr:bytes", <base64_string>)`
- `FxtrRPCHandle` → unwrap to the underlying `RPCHandle[JSONLeaf]`
- `FxtrRPCPending` → unwrap to the underlying `RPCPending[JSONLeaf, ...]`
- Lists, dicts, `RPCTagged` → recurse
- Returns both the encoded value AND the set of FxtrIDs found (collected during the walk)

**Decoding** (`decode_to_fxtr_call_value`): Walk an `IRValue[JSONLeaf]` tree → `FxtrRPCCallValue`:
- `RPCTagged("fxtr:id", ...)` → `FxtrID` (reconstructed from `_raw_data_id`, no provenance)
- `RPCTagged("fxtr:bytes", ...)` → `bytes`
- `RPCHandle` → wrap in `FxtrRPCHandle`
- `RPCPending` → wrap in `FxtrRPCPending`
- Other `RPCTagged` → preserve (application-level tags pass through)
- Lists, dicts → recurse
- Also returns the set of FxtrIDs found

A separate `encode_fxtr_value` / `decode_to_fxtr_value` pair for top-level stream values
(same logic but errors on bare FxtrIDs).

Note: encoding/decoding does NOT clone or discard handles/pendings — it's a structural
transformation. The caller manages ownership. The wrapping of handles into `FxtrRPCHandle` /
`FxtrRPCPending` during decode takes a reference to the owning context, which is passed in.

- [x] Define type aliases and request types (`fxtr_rpc_types.py`)
- [x] Implement `FxtrRPCHandle` shell (dispatch logic comes in Step 3)
- [x] Implement `FxtrRPCPending` shell (pull logic comes in Step 3)
- [x] Implement `encode_fxtr_call_value` (with FxtrID collection)
- [x] Implement `decode_to_fxtr_call_value` (with FxtrID collection)
- [x] Implement `encode_fxtr_value` / `decode_to_fxtr_value` (top-level, no FxtrIDs)
- [x] Unit tests for conversion round-trips, nested structures, mixed handles + FxtrIDs

### Step 2: Arena capability (`fxtr_arena_cap.py`)

Two sides of the arena capability.

**Local side** (`ArenaCapabilityHandle`): Implements `RPCHandle[JSONLeaf]` directly, wrapping
a `ReadableArena`. This is both the capability object AND the Cap-IR handle — no separate
wrapper needed. Managed by `LocalFxtrRPCContext`'s task group and lifecycle (shares a
lightweight base with `_FxtrInternalCapIRRPCHandle` for refcounting, clone/discard,
on_released callback, and teardown).

Exposes a single method via dispatch, mirroring the HTTP `/data/` endpoint pattern:

- `load_data(id_bytes)` → returns a dict with `bytes` (entity CBOR bytes or None),
  `precomp` (dict of precomp relationships for this entity and its deps), and
  `deps` (list of `[id_bytes, dep_kind]` pairs).

This is intentionally minimal — one method, matching what `RemoteArenaAdapter` needs. Bulk
loading can be added later if round-trip latency becomes an issue.

The arena it wraps should be a `copy_to_new_arena` snapshot — the capability owns its own copy
of the relevant entities.

**Remote side** (`RemoteArenaAdapter`): Implements `ReadableArena` by dispatching to a remote
`RPCHandle` (the arena capability). Follows the same pattern as `HttpReadableArena`:
- Maintains `_tracked` set and precomp dicts (`_precomp_unwraps_to`, etc.)
- `load()` calls `load_data` on the handle, ingests precomp, decodes CBOR, tracks child IDs
- `load_flat()` same but returns `FxtrEntityFlatData`
- `unwrap_provenance()`, `c_node_to_context_data()`, `p_node_to_pathroot()`,
  `step_back_p_ctx()` use precomp dicts (same as `HttpReadableArena`)

No local caching in the adapter — the scope handles caching when entities are loaded through
it.

- [x] Implement `ArenaCapabilityHandle` (`RPCHandle[JSONLeaf]` wrapping ReadableArena)
- [x] Implement `RemoteArenaAdapter` (RPCHandle → ReadableArena, following HttpReadableArena)
- [x] Unit tests: round-trip entity data through local arena cap → remote adapter

### Step 3: FxtrRPCHandle and FxtrRPCPending dispatch/pull logic

Fill in the dispatch and pull logic on the types defined in Step 1.

**`FxtrRPCHandle.dispatch(attr_path, request, scope=None)`**:
1. Resolve scope (default: active scope)
2. If `FxtrCall`:
   a. Encode the argument via `encode_fxtr_call_value` — this also collects FxtrIDs
   b. If FxtrIDs were found: create arena from dispatch scope (`copy_to_new_arena` for those
      IDs), create `ArenaCapabilityHandle` wrapping that arena (via the owning context)
   c. Wrap the encoded argument: `RPCTagged("fxtr:call", {"arg": encoded, "arena": handle})`
      (or omit arena if no FxtrIDs)
   d. Dispatch on the underlying `RPCHandle` with the wrapped argument
3. If `FxtrGetAttribute`: dispatch on underlying handle directly
4. Return `FxtrRPCPending` bound to (underlying pending, dispatch scope or None for GetAttribute)

**`FxtrRPCHandle.dispatch_and_wait`**: Similar but blocking. Pulls result immediately.

**`FxtrRPCPending.pull(into_scope=None, copy_into_scope=False)`**:
1. Check dispatch scope alive (if present) via `scope.check_alive()`
2. Resolve destination scope (default: active scope)
3. Validate scope constraints:
   - No copy: `into_scope` descends from both dispatch scope (if present) and connection scope
   - Copy: dispatch scope (if present) and connection are alive
4. Pull from underlying `RPCPending` → `IRValue[JSONLeaf]`
5. Detect `RPCTagged("fxtr:result", ...)` wrapper → extract result + arena handle
6. Decode via `decode_to_fxtr_call_value` → `FxtrRPCCallValue` with FxtrIDs and collected ID set
7. If arena handle present: create `RemoteArenaAdapter`
8. For each FxtrID in result:
   - First check if already in dispatch scope (forwarding optimization — skip remote fetch)
   - If not copy: attach remote arena to `into_scope` via `unsafe_lazily_attach_from_arena`
   - If copy: create temp child scope of connection scope, attach remote arena there,
     `copy_from_scope` each FxtrID into `into_scope`, close temp scope
9. Return decoded `FxtrRPCCallValue`

**`FxtrRPCPending.wait_until_teardown()`**: Delegates to underlying pending. (For now, does
not proactively detect dispatch scope exit — uses `check_alive()` on operations instead.
Scope exit notification can be added as a follow-up.)

**`FxtrRPCPending.as_handle()`**: Call underlying `as_handle()`, wrap in `FxtrRPCHandle`.

**`clone()`, `discard()`**: Delegate to underlying.

- [x] Implement `FxtrRPCHandle.dispatch` (with arena creation, encoding, wrapping)
- [x] Implement `FxtrRPCHandle.dispatch_and_wait`
- [x] Implement `FxtrRPCPending.dispatch` (same scope binding logic as FxtrRPCHandle.dispatch)
- [x] Implement `FxtrRPCPending.pull` (scope validation, decoding, arena mounting/copying)
- [x] Implement forwarding optimization (check dispatch scope before remote fetch)
- [x] Implement `as_handle`, `clone`, `discard`, `wait_until_teardown`
- [ ] Scope constraint validation tests (deferred to Step 4/6 — needs LocalFxtrRPCContext)
- [ ] `copy_into_scope` vs lazy mount tests (deferred to Step 4/6 — needs LocalFxtrRPCContext)

### Step 4: LocalFxtrRPCContext and internal Cap-IR handles (`fxtr_rpc_local.py`)

`LocalFxtrRPCContext` owns a task group and a connection-level `FxtrScope`. Its `wrap(obj)`
creates Cap-IR handles that speak the fxtr wire encoding internally.

```python
class LocalFxtrRPCContext(anyio.AsyncContextManagerMixin):
    scope: FxtrScope  # connection-level scope

    def wrap(self, obj) -> FxtrRPCHandle:
        """Wrap a Python object as an FxtrRPCHandle."""
        ...
```

#### Two internal Cap-IR handle types

Both implement `RPCHandle[JSONLeaf]` directly and share a lightweight base for lifecycle
management (refcounting, clone/discard, on_released callback, teardown on context exit).

**`_FxtrInternalCapIRRPCHandle`**: Wraps a fxtr-aware Python object. Implements path walking
(like `LocalHandle`'s `_walk_to_target` / `_step_into`) and the fxtr dispatch pipeline:

- On `Call` with `RPCTagged("fxtr:call", ...)`: unwraps the fxtr call encoding, creates a
  per-call `FxtrScope`, mounts argument entities via arena capability, runs user handler,
  encodes result with return arena capability.
- On `GetAttribute`: path walks normally, encodes result if it contains FxtrIDs.
- On plain `Call` (no fxtr tag): path walks and calls normally (pass-through for non-fxtr
  callers, like arena capability dispatches from `RemoteArenaAdapter`).

**`ArenaCapabilityHandle`** (from Step 2): Wraps a `ReadableArena`, implements
`RPCHandle[JSONLeaf]` with a single `load_data` method. Same lifecycle base. Used for both
argument and return arena capabilities.

Both handle types are real Cap-IR handles — they can be sent over a Cap-IR stream, pipelined,
cloned, and discarded normally. `FxtrRPCHandle` wraps whichever `RPCHandle[JSONLeaf]` it
receives, whether that's an `_FxtrInternalCapIRRPCHandle`, a `JSONTransportHandle`, or
anything else.

#### Per-call scope lifetime

The per-call scope must stay alive after the handler returns, because it backs the return
arena capability (`ArenaCapabilityHandle`). The scope is held open in a background task:

```python
# Pseudocode
async def _hold_call_scope(call_scope, discard_event, ctx_teardown_event):
    async with call_scope:
        # ... set up argument arena, run handler, create return arena ...
        # Hold scope open until cleanup signal
        await wait_for_either(discard_event, ctx_teardown_event)
```

The background task exits (closing the scope) when either:
- The `on_released` callback fires on the return `ArenaCapabilityHandle` (all refs discarded)
- The `LocalFxtrRPCContext` exits (forcible cleanup)

#### Context exit

On exit: cancel all background tasks (which closes all per-call scopes). All handles created
from this context are considered torn down.

- [x] Implement shared handle lifecycle base (refcounting, clone/discard, on_released, teardown)
- [x] Implement `_FxtrInternalCapIRRPCHandle` (path walking + fxtr encode/decode pipeline)
- [x] Implement `LocalFxtrRPCContext` (task group, scope, wrap method)
- [x] Implement per-call scope lifecycle (background task + discard notification)
- [x] Implement forced cleanup on context exit
- [x] Tests: local wrap + dispatch + pull round-trip

### Step 5: RemoteFxtrRPCContext (`fxtr_rpc_stream.py`)

Lifts a `ObjectStream[IRValue[JSONLeaf]]` to a `FxtrRPCValue` stream.

```python
class RemoteFxtrRPCContext(anyio.AsyncContextManagerMixin):
    def __init__(self, cap_ir_stream: ObjectStream[IRValue[JSONLeaf]]):
        ...

    scope: FxtrScope  # connection-level scope

    async def send(self, value: FxtrRPCValue) -> None: ...
    async def receive(self) -> FxtrRPCValue: ...
```

Internally also owns a `LocalFxtrRPCContext` (or at least a `LocalRPCContext`) for creating
arena capability handles for outgoing calls and for wrapping local objects that the user wants
to expose through the stream.

**`send(value)`**:
1. Encode via `encode_fxtr_value` (errors on bare FxtrIDs)
2. Send on the Cap-IR stream

**`receive()`**:
1. Receive from the Cap-IR stream
2. Decode via `decode_to_fxtr_value` (wraps handles in `FxtrRPCHandle`, errors on bare FxtrIDs)
3. Return

No background receive task — just calls through to the underlying stream's `receive()`.

**Abort behavior**: If the Cap-IR stream closes or errors, the next `send`/`receive` will
propagate the error. The connection scope should be torn down. This happens naturally:
when the `RemoteFxtrRPCContext` exits, its scope exits, which tears down all per-call scopes
and pending values.

Optionally, `RemoteFxtrRPCContext` could provide a `wrap(obj)` method that delegates to its
internal `LocalFxtrRPCContext`, so users can wrap local objects and send them through the
stream. But this isn't strictly necessary for the first version — users can create their own
`LocalFxtrRPCContext` separately.

- [x] Implement `RemoteFxtrRPCContext`
- [x] Implement `send` / `receive` with conversion
- [x] Test with JSON transport ObjectStream loopback

### Step 6: Integration tests (`test_fxtr_bridge.py`)

**Tests using `LocalFxtrRPCContext` (no transport):**

The primary test category. Tests the scope and arena logic without transport complexity.

- [ ] Basic entity send: dispatch a call with a FxtrID argument, verify callee can load it
- [ ] Basic entity receive: callee returns a FxtrID, caller can load after pull
- [ ] Multiple entities: call with several FxtrIDs sharing deps
- [ ] Forwarding: send entity, callee returns it, verify no redundant remote fetch
- [ ] `copy_into_scope`: pull with copy, verify entity loadable after connection closes
- [ ] Lazy pull scope constraint: pull fails if into_scope doesn't descend from both scopes
- [ ] Teardown on scope exit: pending tears down when dispatch scope exits
- [ ] Teardown on connection close: pending tears down when context closes
- [ ] Nested calls: callee makes sub-calls, verify scope isolation
- [ ] Handles + entities: return value contains both a handle and FxtrIDs
- [ ] `GetAttribute` with entity results (no dispatch scope, only connection scope constraint)
- [ ] Large entity tree: send root only, verify lazy loading (only loads what callee accesses)
- [ ] Entity with children (Child[T], LinkedEntity[T]): verify inline entities survive the trip
- [ ] Per-call scope cleanup: verify scope closes when pending is discarded

**Lifetime edge case tests:**

- [ ] Dispatch scope closes immediately after dispatch (before callee runs): callee should
  still be able to load arg entities because keep_alive holds the scope open until
  release_scope is called
- [ ] Pipeline through multiple dispatches without pulling: step 1 stores a new entity that
  references an arg entity, step 2 loads both entities (the new one and the original arg
  through the reference). Tests that arg entities remain accessible through pipelining
  even after release_scope
- [ ] Return arena cap discard cleans up detached store: verify no resource leaks

**Tests using `RemoteFxtrRPCContext` over JSON transport:**

Subset of above through actual serialization:

- [ ] Basic entity round-trip through JSON ObjectStream
- [ ] Entities with bytes fields
- [ ] Handle + entity mixed values
- [ ] Handles received from stream, dispatched with entities

**Test infrastructure:**

```python
# Simple test entity types
@dataclass(frozen=True)
class TestEntity(DataclassEntity, fxtr_type=define_type(TEST_NS, "test")):
    value: str

@dataclass(frozen=True)
class TestTreeNode(DataclassEntity, fxtr_type=define_type(TEST_NS, "tree_node")):
    label: str
    children: list[Child[TestTreeNode]]
```

### Step 7: Update documentation

- [x] Update `rpc/LOCAL_CODE_OVERVIEW.md` with new modules
- [x] Update `CODE_OVERVIEW.md` with the fxtr bridge section

## Wire Encoding Convention

When `FxtrRPCHandle.dispatch` sends a call with FxtrIDs, the argument is wrapped:

```
RPCTagged("fxtr:call", {
    "arg": <encoded_argument>,  # IRValue[JSONLeaf] with FxtrIDs as tagged values
    "arena": <arena_capability_handle>,  # RPCHandle for loading entities
})
```

When no FxtrIDs are present, the wrapper can be omitted (just the raw encoded value). The
receiving side detects wrapping by checking for the tag.

When the callee returns with FxtrIDs, the result is similarly wrapped:

```
RPCTagged("fxtr:result", {
    "value": <encoded_result>,
    "arena": <arena_capability_handle>,
})
```

For `GetAttribute`, there is no argument arena. The result may still be wrapped.

## Open Questions

1. **Scope exit notification**: Using `scope.check_alive()` checks on operations for now. The
   RPC context's own teardown is communicated naturally via Cap-IR handle teardown. Proactive
   scope-exit notification for `wait_until_teardown` can be added as a follow-up.

2. **FxtrID encoding format**: Base64 for JSON transport; native bytes for future CBOR
   transport. Tag-based encoding keeps this a transport-level detail.

3. **Derived node handling in RemoteArenaAdapter**: Follow the `HttpReadableArena` pattern —
   use precomp dicts for provenance relationships, handle derived nodes the same way.

4. **Arena capability lifetime**: Use the discard-notification mechanism (prerequisite
   enhancement) to detect when the caller releases the pending. Fire callback → close per-call
   scope. On `FxtrRPCContext` exit, forcibly close all per-call scopes regardless of discard
   state.
