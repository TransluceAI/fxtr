# JSON Transport — Design Discussion Notes (Round 2)

Notes from the design discussion after writing the initial PROTOCOL.md spec and design notes.
These capture decisions made and their rationale, for reference during implementation.

## Decision: Teardown implies release

When the exporter sends `teardown` or `broken` for an export, the export is implicitly released.
The importer does not need to send a separate `release` message. Rationale: if you already know
that all future accesses to the object will fail, there's no reason to hold onto the reference.

If `release` and `teardown` cross in flight (one side sends release, the other sends teardown
simultaneously), both sides should tolerate and ignore the stale message. At most one of
{release, teardown} is the "real" cleanup; the other is a no-op.

The import entry should be kept alive (in a "torn down" state) on the importer's side as long as
local clones exist, so that `wait_until_teardown()` callers can receive the teardown reason. But
no `release` message needs to be sent back.

## Decision: Teardown is only for handle exports

Teardown/broken messages are only sent for handle exports (via `["export", ...]` expressions).
They are never sent for:
- **Push results**: transport-owned; they resolve, reject, or the connection breaks via `abort`.
- **Submit results**: auto-released after resolve/reject; the sender can't do anything with the
  import anyway.
- **Promise exports** (`["promise", ...]`): auto-pulled, so `reject` always reaches the recipient.
  The only time teardown would fire but reject wouldn't is if we never pulled — but promise exports
  are always auto-pulled.

The key distinction: handles have no "reject" mechanism, so teardown is the only way to signal
"this object is dead." Pending values always have reject available via auto-pull.

At the IR level, `RPCPending.wait_until_teardown()` can still fire (e.g. when the transport
breaks, or the pending is discarded), but the transport never needs to send a teardown *message*
for pending values. The teardown is handled locally.

## Decision: Promises delivered as RPCPending (not resolved first)

Cap'n'Web resolves promise expressions before delivering the enclosing message to the application.
Cap-IR does NOT do this. The transport delivers `["promise", exportId]` as an `RPCPending` value
directly in the IRValue tree, without waiting for resolution.

Rationale: the ObjectStream should be a 1:1 map of IRValue types — whatever shape of value was
sent (in terms of the abstract IR types) is the shape that's received. Resolution is handled at
the application/IR boundary (e.g. `LocalHandle._resolve_pending_in_value`), not by the transport.

This means the transport never needs to "batch and await" promise resolutions. Promise exports
still get proactive resolve/reject from the sender; the RPCPending resolves asynchronously when
that message arrives.

## Decision: Import/pipeline expression evaluation model

Both `["import", ...]` and `["pipeline", ...]` expressions are handled the same way, with one
difference in the final wrapping:

- **Without path/call**: look up our export, deliver as RPCHandle (`import`) or RPCPending
  (`pipeline`). For `import`, if the export is a pending value, `as_handle()` is used to coerce.

- **With path and/or call**: both dispatch on our export (producing an RPCPending). `import`
  calls `as_handle()` on the result to deliver an RPCHandle. `pipeline` delivers the RPCPending
  directly.

In practice, `["import", id]` usually references negative IDs (handle exports) and
`["pipeline", id]` usually references positive IDs (push results), so the type usually matches
naturally.

## Decision: Transport architecture (two layers)

The JSON transport should be structured as two layers:

**Layer 1: `JSONTransportContext`** (the core)
- Constructor takes: `ObjectStream[JSONTypes]`, `main_interface: RPCHandle`
- Owns: read loop, import/export tables, message encoding/decoding, background tasks
- Exposes: `.remote_main` → `RPCHandle` for import 0 (the remote's main interface)
- Generic — no ObjectStream semantics built in

**Layer 2: ObjectStream helper** (separate, testable independently)
- Creates a local handle that implements `send(value)` → passes as `main_interface` to Layer 1
- Calls `.remote_main.send(value)` for outgoing values
- Produces `ObjectStream[IRValue[JSONLeaf]]` for the application

This separation means the core transport is testable without the ObjectStream wrapper, and the
ObjectStream wrapper can potentially be written and tested against a mock transport.

## Expression encoding notes (for the Devaluator equivalent)

When encoding an `IRValue` for sending:

- **Bounce-back detection**: If an `RPCHandle` is a `JSONTransportHandle` pointing back at
  *this same connection's* imports, emit `["import", importId]`. The handle points at their
  export, so we reference it directly.

- **Reverse export lookup**: Track `id(python_object)` → export ID. When encoding a handle or
  value we've already exported, reuse the same export ID (incrementing refcount) via
  `["export", existingExportId]`.

- **RPCPending always gets a new ID**: Even if the same pending was exported before, always
  allocate a fresh export ID for promise expressions. (If an existing ID were reused, the
  recipient could miss the resolution.)

- **RPCPending bounce-back**: If an `RPCPending` is a `JSONTransportPending` pointing at this
  connection's imports, emit `["pipeline", importId]`.
