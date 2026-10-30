# Stage 5 Part 3: Simplified JSON Transport

## Goal

A second JSON transport implementation that is a **drop-in replacement** for
`JSONTransportContext` at the `RPCHandle`/`RPCPending` ABC level, but trades efficiency
for simplicity. The goal is for the implementation to be simple enough that its
correctness is obvious by inspection, making it useful as a reference implementation
and as a second transport for differential testing.

## Design principles

1. **No promise pipelining.** Calls are fully resolved before sending. No pending values
   cross the wire.
2. **No encoding rollback.** If encoding fails, we haven't touched any tables yet. If
   decoding/evaluation fails, we abort the connection.
3. **Multiple calls in flight** (each identified by a call ID), to avoid deadlocks when
   both sides call each other.
4. **Handles still passed by reference** via export/import tables with refcounting. Same
   negative-ID (exporter-chosen) scheme as the full protocol.
5. **ObjectStream layer is out of scope.** Can be added later by reusing the same
   ObjectStream wrapper from the full transport.

## Wire protocol

### Messages

```
["call", callId, importId, attrPath, argExpr]
["get", callId, importId, attrPath]
["result", callId, resultExpr]
["error", callId, errorExpr]
["release", importId, refcount]
["teardown", exportId, reasonExpr]
["broken", exportId, errorExpr]
["abort", errorExpr]
```

- `callId`: Positive integer, allocated sequentially by the caller (each side has its own
  counter, starting from 1). Used only to match results to calls — NOT entered into the
  import/export table.
- `importId`: The caller's import ID for the target handle (the recipient's export ID).
- `attrPath`: JSON array of strings/ints (the attribute path for the dispatch).
- `argExpr`: An expression for the call argument (see below). Can be any expression
  including `null` (which represents calling with `None`).
- `errorExpr`: An error expression, typically `["error", typeName, message]`.
- `refcount`: Total number of times this import was introduced (via `["handle", ...]`
  expressions), used to avoid the release/re-export race.

### Expressions

Expressions appear in `argExpr`, `resultExpr`, `reasonExpr`, and `errorExpr` positions.
They are always **fully resolved** — no pending values, no pipelining references.

```
JSON literals          — pass through (numbers, strings, booleans, null)
{"key": expr, ...}     — object, values are expressions
[["elem1", "elem2"]]   — array expression (inner elements are expressions)
["handle", exportId]   — a handle, identified by the sender's export ID
["import", importId]   — a handle the recipient already exported to us
["tag", tagExpr, valExpr] — tagged value
["error", typeName, message] — error/exception value
["inf"], ["-inf"], ["nan"]  — special floats
```

**`["handle", exportId]`**: The sender is exporting a handle, using a negative export ID.
In principle, a sender may reuse an export ID if it is re-exporting the same underlying
object (bumping the refcount), and receivers should handle this correctly. However, senders
are not required to deduplicate, and the current implementation always assigns a fresh
export ID for each handle occurrence.

**`["import", importId]`**: References a handle the **recipient** previously exported to
the sender. The sender uses their import ID (which is the recipient's export ID). No
refcount change — this is a back-reference, not a new introduction. On the recipient side,
this resolves to the local object behind that export.

> Note: We use `"handle"` instead of `"export"` to avoid confusion with the full protocol,
> since this simplified protocol has different semantics (no promise expressions, no
> pipeline expressions).

### Message semantics

**Call flow:**
1. Caller sends `["call", callId, importId, attrPath, argExpr]`.
2. Recipient looks up `importId` in their export table, dispatches the call locally.
3. Recipient waits for the result to fully resolve (including any pendings in the result tree).
4. Recipient sends `["result", callId, resultExpr]` or `["error", callId, typeName, msg]`.

**`get` vs `call`:**
- `["get", ...]` dispatches a `GetAttribute` — looks up the attribute at `attrPath`.
- `["call", ...]` dispatches a `Call(argExpr)` — calls the method at `attrPath` with the
  given argument. `argExpr` can be any expression, including `null` (which calls with `None`).

**Teardown/broken:**
- When a locally-exported handle tears down, the exporter sends `["teardown", ...]` or
  `["broken", ...]`.
- These implicitly release the export — the importer does not send a separate `release`.
- If a `release` crosses with a `teardown`/`broken`, both sides tolerate the stale message.

**Abort:**
- On any internal error (encoding failure after table mutation, decoding failure, unknown
  message type, etc.), the side that detected the error sends `["abort", errorExpr]` and
  closes the stream.
- All pending calls on both sides fail with `RPCResourceBrokenError`.

## Implementation

### File: `fxtr2/rpc/simple_json_transport.py`

### Module docstring

The module docstring should contain the full wire protocol specification (messages,
expressions, semantics), largely copied from this plan's "Wire protocol" section. This
makes the implementation file self-contained — a reader shouldn't need to cross-reference
the plan to understand the protocol.

### Classes

**`SimpleJSONTransportContext`** — the connection object.
- Owns the read loop, export/import tables, background task group.
- Used as an async context manager. On entry, starts the read loop. On exit, aborts.
- `remote_main: SimpleJsonHandle` — the remote side's main interface (import 0).
- Methods: `_send_message`, `_handle_call`, `_handle_result`, `_handle_error`,
  `_handle_release`, `_handle_teardown`, `_handle_broken`, `_handle_abort`.

```python
class SimpleJSONTransportContext:
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, ...): ...

    # The remote side's main interface.
    remote_main: SimpleJsonHandle

    # Tables.
    _exports: dict[int, ExportEntry]
    _imports: dict[int, ImportEntry]

    # Object → export ID reverse lookup (for reusing IDs).
    _export_by_identity: dict[int, int]  # id(obj) → export ID

    # ID counters.
    _next_export_id: int  # starts at -1, goes down
    _next_call_id: int    # starts at 1, goes up

    # Pending calls awaiting results.
    _pending_calls: dict[int, anyio.Event]  # callId → event
    _call_results: dict[int, ...]           # callId → result or error
```

**`ExportEntry`** — tracks a locally-exported handle.
```python
@dataclasses.dataclass
class ExportEntry:
    value: RPCHandle[JSONLeaf]      # the local handle (or any dispatchable)
    remote_refcount: int            # how many times introduced to remote
    teardown_task_started: bool     # whether we're watching for teardown
```

**`ImportEntry`** — tracks a remotely-imported handle.
```python
@dataclasses.dataclass
class ImportEntry:
    handle: SimpleJsonHandle            # the local stub
    local_refcount: int             # how many local clones exist
    remote_refcount: int            # how many times introduced by remote
    torn_down: bool                 # whether teardown/broken received
```

**`SimpleJsonHandle(RPCHandle[JSONLeaf])`** — remote handle stub.
- Holds a reference to the context + import ID.
- `dispatch()`: resolves all pendings in the argument, encodes, sends `["call", ...]`,
  returns a `SimpleJsonPending` backed by waiting for the result.
- `dispatch_and_wait()`: same as `dispatch()` then `await pending.pull()`.
- `clone()`: increments `ImportEntry.local_refcount`, returns new `SimpleJsonHandle`.
- `discard()`: decrements `local_refcount`; if it hits 0 and not torn down, sends `release`.
- `wait_until_teardown()`: waits for teardown/broken/abort.

**`SimpleJsonPending(RPCPending[JSONLeaf, IRValue[JSONLeaf]])`** — pending call result.
- Backed by an `anyio.Event` + a result slot.
- `pull()`: waits for the event, returns a clone of the result.
- `as_handle()`: returns a `SimpleJsonPendingHandle` that waits for resolution before
  dispatching (same pattern as `LocalPendingHandle`).
- `dispatch()`: waits for resolution, then dispatches on the resolved value.
- `clone()`: refcounted (shares the underlying result slot).
- `discard()`: decrements refcount; cleans up resolved value when last clone discards.

**`SimpleJsonPendingHandle(RPCHandle[JSONLeaf])`** — pending-to-handle wrapper.
- Same pattern as `LocalPendingHandle` from `basic_local_caps.py`.
- All operations wait for the underlying pending to resolve, then delegate to the
  resolved handle.
- Special encoding behavior: if this handle is passed as an argument to a call on the
  **same** connection, the encoder waits for it to resolve first and checks that it
  resolved to a `SimpleJsonHandle` on this connection (in which case it uses `["import", ...]`
  to refer to it). If it's from a **different** connection, it's exported directly as a
  new `["handle", ...]`.

### Concurrency details

**Send serialization**: Multiple concurrent tasks (incoming call handlers, teardown
watchers, outgoing dispatches) may all call `_send_message`. The context must use a lock
(e.g. `anyio.Lock`) to serialize sends, ensuring messages are not interleaved at the
stream level.

**`dispatch()` is non-blocking but encoding is async**: The `RPCHandle.dispatch()` ABC
requires returning immediately. But our encoding needs to resolve pendings (async) before
sending. So `SimpleJsonHandle.dispatch()` must:
1. Allocate the call ID **synchronously** (so IDs are sequential in dispatch order).
2. Clone the argument synchronously (caller can mutate/discard after dispatch returns).
3. Spawn a background task that: resolves pendings in the cloned arg, encodes, sends the
   `["call", ...]` message, waits for the result, and settles the `SimpleJsonPending`.

This means multiple dispatches can interleave on the wire. That's acceptable — we don't
guarantee call ordering (documented as a known limitation, same as the full protocol).

### Lifetime / copy rules

The transport must respect Cap-IR's ownership model:
- **Dispatch arguments**: The argument is cloned at dispatch time. The caller can mutate or
  discard their copy immediately after `dispatch()` returns. Since we resolve all pendings
  before encoding, the "clone" happens naturally: we pull pendings (which returns copies),
  and encoding consumes the resolved tree.
- **Dispatch results**: The result returned by `dispatch_and_wait` on the export side is
  already an independently-owned copy (per the `dispatch_and_wait` contract). The transport
  encodes it and sends it; the decoded result on the import side is a fresh tree.
- **`pull()` on `SimpleJsonPending`**: Must return a clone of the resolved value (not the
  stored value itself), since multiple pulls should return independent copies.
- **Handle passing**: Handles in expressions are always cloned on the receiving side
  (new `SimpleJsonHandle` for `["handle", ...]`, cloned local handle for `["import", ...]`).

### Encoding (argument/result → expression)

The encoder takes an `IRValue[JSONLeaf]` and produces a JSON expression. It operates in
two phases to avoid needing rollback:

**Phase 1: Prepare.** Walk the IR value tree:
- Resolve all `RPCPending` values (await them).
- For `SimpleJsonPendingHandle` on this connection: await resolution, get the underlying handle.
- For each handle encountered:
  - If it is a `SimpleJsonHandle` on this connection's *import* table: skip (will encode as
    `["import", importId]` — no table mutation needed).
  - Otherwise: allocate a new negative export ID, **clone the handle** (so the export table
    owns its own reference), and record the clone + new ID. Every non-back-reference handle
    always gets a fresh export — there is no dedup or re-export (see note on `["handle"]`).
- Validate that all values are encodable (no unknown types).
- If anything fails during this phase, raise the error. Nothing has been mutated. Any
  clones made speculatively for new exports must be discarded on failure.

**Phase 2: Commit and encode.** Only reached if phase 1 succeeded:
- Add new exports to the export table (using the cloned handles from phase 1).
- Start teardown watchers for new exports.
- Produce the JSON expression.

### Decoding (expression → IR value)

The decoder takes a JSON expression and produces an `IRValue[JSONLeaf]`. On any error,
the connection aborts.

- `["handle", exportId]`: Look up or create an `ImportEntry`. If entry exists, bump
  `remote_refcount` and `local_refcount`, return a new `SimpleJsonHandle`. If new, create
  entry with refcounts = 1.
- `["import", importId]`: Look up our export table at `importId`. Clone the exported
  handle (the caller gets their own copy). No refcount changes to the import/export tables.
- Other expressions: decode recursively.

### Incoming call handling

When `["call", callId, exportId, attrPath, argExpr]` or
`["get", callId, exportId, attrPath]` is received:
1. Look up `exportId` in the export table.
2. For `call`: decode `argExpr` into an `IRValue[JSONLeaf]`, dispatch as `Call(decoded_arg)`.
   For `get`: dispatch as `GetAttribute`.
3. Await the result (dispatch_and_wait).
4. Encode the result as an expression (phase 1 + phase 2).
5. Send `["result", callId, resultExpr]`.
6. On error at any point: send `["error", callId, errorExpr]`.

Steps 2-6 run in a background task so multiple calls can be processed concurrently.

### Teardown watching

When a handle is exported, start a background task that awaits
`handle.wait_until_teardown()`. When it completes:
- Normal return: send `["teardown", exportId, reasonExpr]` and remove the export entry.
- Exception: send `["broken", exportId, errorExpr]` and remove the export entry.

### Connection lifecycle

1. **Start**: Both sides provide their "main" handle. Each side exports it as export ID 0
   and creates import 0 for the remote main.
2. **Running**: The read loop processes incoming messages, dispatching to handler methods.
   Outgoing messages are sent via `_send_message`.
3. **Shutdown**: On context exit, send `["abort", ...]`, cancel the task group, fail all
   pending calls with `RPCResourceBrokenError`, and notify all import teardown waiters.

## Testing

### Add to `test_transport.py`

Add `simple_json_loopback_transport` fixture alongside the existing `json_loopback_transport`.
Same pattern: create memory stream pair, `SimpleJSONTransportContext` on each side, yield
`remote_main`. All existing parameterized tests run against it.

### Optional: `test_simple_json_transport.py`

If needed, a small file for edge cases specific to the simplified transport (e.g. verifying
that encoding never sends pending values, that `SimpleJsonPendingHandle` correctly waits before
encoding).

## Steps

- [x] 1. Implement `SimpleJSONTransportContext`, `SimpleJsonHandle`, `SimpleJsonPending`,
         `SimpleJsonPendingHandle` in `fxtr2/rpc/simple_json_transport.py`.
- [x] 2. Add `simple_json_loopback_transport` to `tests/test_rpc/test_transport.py` and
         verify all parameterized tests pass.
- [ ] 3. Add any simplified-transport-specific tests if needed.
- [x] 4. Update `CODE_OVERVIEW.md` and `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md`.
