# Stage 5 Part 2: JSON Transport — Overview

## What we're building

A JSON transport for Cap-IR that multiplexes capabilities over a bidirectional JSON message
stream. The public API accepts an `anyio.abc.ObjectStream[JSONTypes]` (a channel that can
already send/receive JSON-serializable values) and produces an
`anyio.abc.ObjectStream[IRValue[JSONLeaf]]` — a channel through which IR values (including
handles, pendings, and tagged values) can be sent and received transparently.

Under the hood, this is implemented as a variant of the
[Cap'n'Web protocol](https://github.com/cloudflare/capnweb/blob/main/protocol.md). We match
Cap'n'Web's semantics as closely as possible, with a small set of documented changes to support
our IR's type system (tags, single-argument calls, explicit pending values) and lifecycle model
(teardown notifications). The ObjectStream API is implemented as a convention on top of the
main interface (ID 0), not as a custom protocol extension.

Files:
- `fxtr2/rpc/PROTOCOL.md` — wire protocol specification
- `fxtr2/rpc/json_transport.py` — implementation
- `tests/test_rpc/test_transport.py` — transport-parameterized integration tests (extend existing)
- `tests/test_rpc/test_json_transport.py` — JSON-specific high-level tests
- `tests/test_rpc/test_json_transport_wire.py` — raw wire-level protocol tests (in progress)

## Steps

### 1. Wire protocol spec
Write `fxtr2/rpc/PROTOCOL.md`: a precise specification of our wire protocol, based on Cap'n'Web,
with our modifications clearly documented. This is the source of truth for what messages the
transport sends and receives.

Status: **done**

### 2. Wire-facing API design
Write a detailed design spec (`plans/phase3/stage5_part2_json_transport_api.md`) for the
Python-side implementation:
- `JSONTransportContext` (or similar): the connection object that owns the read loop, import/export
  tables, and background tasks. Implements `ObjectStream[IRValue[JSONLeaf]]`.
- `JSONTransportHandle(RPCHandle[JSONLeaf])`: remote handle stub. Holds connection + import ID.
  dispatch/dispatch_and_wait send protocol messages; clone/discard manage local refcounts and send
  release; wait_until_teardown blocks on teardown/broken from the remote side.
- `JSONTransportPending(RPCPending[JSONLeaf, ...])`: remote pending stub. Backed by a future that
  resolves when resolve/reject arrives. pull() sends a pull message.
- Import table entries: refcounting (local + remote), future for resolution, teardown state.
- Export table entries: the exported IR value, refcount, whether being resolved.
- Expression encoder/decoder: `IRValue[JSONLeaf]` ↔ JSON expression trees, using the
  Importer/Exporter interface pattern from Cap'n'Web.
- Lifecycle: how the connection starts, processes messages, and tears down; how exports are
  watched for local teardown; how connection close propagates to all imports.

All of this must flow through the `cap_ir.py` ABCs. The transport never touches local caps
internals (`basic_local_caps.py`).

Status: **done**

### 3. Implement the JSON transport
Implement `fxtr2/rpc/json_transport.py` per the API design.

Status: **done**

### 4. Tests
- Add a JSON loopback transport fixture to `test_transport.py` (per the test architecture described
  in the Part 1 plan: create an in-memory ObjectStream pair, lift to JSON, lift to IRValue, pass
  handle through).
- All existing transport-parameterized tests should pass with both identity and JSON transports.
- Add JSON-transport-specific tests if needed (e.g. serialization edge cases, refcounting, reconnect
  after release-in-flight).

Status: **mostly done** (see current state below)

### 5. Update docs
- Update `fxtr2/rpc/LOCAL_CODE_OVERVIEW.md` (include call-ordering limitation)
- Update `CODE_OVERVIEW.md`
- Update test counts

Status: **done**

## Current state (as of 2026-04-02)

### What's working

165+ tests pass (all RPC tests):
- 43 local caps unit tests (`test_local_caps.py`)
- 102 transport-parameterized tests (`test_transport.py`) — both identity and JSON loopback
- 20 JSON-specific high-level tests (`test_json_transport.py`) — serialization, handle passing,
  abort, teardown notifications, pipelining, edge cases
- 13 raw wire-level tests (`test_json_transport_wire.py`) — all xfailed pending harness
  teardown fix (test logic passes, teardown hangs)

The core transport implementation works: dispatching, pipelining, handle passing, teardown
notifications, expression encoding/decoding, abort, concurrent dispatches.

### What's remaining

1. **Wire test harness teardown** (`test_json_transport_wire.py`): All 13 tests pass their
   assertions but call `pytest.xfail()` to avoid hanging on context exit. The root cause was
   identified: `_do_abort` closed the stream but didn't cancel the task group scope, so the
   read loop stayed blocked on `receive()` forever. Fix: added `tg.cancel_scope.cancel()` in
   the context manager's `finally` block. The xfail markers on the wire tests need to be removed
   now that this is fixed. See `stage5_part2_json_transport_api_implnotes_2.md` for the
   original debugging notes.

2. **Bugs and improvements** identified in a code review against the API design. See
   `stage5_part2_json_transport_impl_review.md` for the full list. Key issues:
   - Evaluation rollback doesn't handle re-introduction correctly (B1, B2)
   - Encoding rollback doesn't track re-export refcount bumps (B3)
   - `_do_abort` stream close missing try/except (B4)
   - `_TrivialPending.clone()` messy / `_TrivialPending` doesn't detect abort (I1, I2)
   - Duplicated local dispatch logic across 4 methods (I3)

3. **Layer 2 (ObjectStream helper).** Explicitly deferred to a later stage per the API design.

### Implementation notes

Several issues were discovered and fixed during implementation. These are documented in detail in:
- `stage5_part2_json_transport_impl_review.md` — code review against design (bugs, missing
  features, improvement opportunities)
- `stage5_part2_json_transport_api_implnotes.md` — sync dispatch / deferred sends, pipelining
  for PendingHandle, exception types across the wire, pull-for-teardown, discard racing
- `stage5_part2_json_transport_api_implnotes_2.md` — wire test harness teardown issue
