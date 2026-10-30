# Stage 5: Cap-IR — Capability-Passing RPC

## Goal

Build an RPC system based on an extensible variant of the [Cap'n'Web][] protocol. The system
uses an intermediate representation ("Cap-IR") that decouples the application-facing API from the
transport layer, supporting capability-passing, promise pipelining, and bidirectional communication.

Key design decisions (vs Cap'n'Web):
- **Tag-based extensibility** instead of hard-coded JS types (Date, Headers, etc.)
- **No built-in streams or remap** — these are handled at the application level via capability objects
- **Single-argument calling convention** — the protocol passes one `IRValue` per call; applications
  choose their own convention for packing positional/keyword args
- **Explicit pending values** rather than implicit promises, to fit Python's async model
- **`wait_until_teardown`** on handles and pending values for proactive liveness detection

[Cap'n'Web]: https://github.com/cloudflare/capnweb/blob/main/protocol.md

## IR types (defined in `fxtr2/rpc/cap_ir.py`)

`IRValue[Leaf]` is parameterized over transport-specific leaf types:
- Leaf values, None, lists, dicts, exceptions
- `RPCTagged` — application-defined semantic tags (inspired by CBOR tags)
- `RPCHandle` — pass-by-reference capability objects with dispatch, clone, discard, teardown
- `RPCPending` — pipelineable pending results with pull, as_handle, dispatch, teardown

## Implementation stages

### Part 1: Local transport (`basic_local_caps.py`)
Implement the IR entirely in-process: wrap Python objects as `LocalHandle`, run dispatches via
a task group, and return `LocalPending` values backed by anyio futures. No serialization or
message passing — the "transport" is just direct Python calls. This validates the IR semantics
and establishes the test suite.

### Part 2: JSON transport
Implement a JSON-based transport that serializes `IRValue[JSONLeaf]` into Cap'n'Web-style
messages over a bidirectional message stream. Handles import/export tables, push/pull/resolve
messages, and stub proxying. Tests reuse the same parameterized suite from Part 1.

### Part 3: Conversion logic (design step)
Design the layer that converts between application-level Python types and `IRValue`. This
includes tag-based encoding/decoding, schema-driven dispatch, and integration with fxtr's
existing entity/type system. Sparse for now — will be fleshed out once Parts 1-2 are stable.

## Testing strategy

Tests are parameterized over the transport. Each transport provides a fixture that takes a
"root capability" (an object implementing some test interface) and returns a handle to it:
- **Local/identity transport**: wraps the object in a `LocalHandle` directly
- **JSON transport** (Part 2): sets up a local loopback message queue, pushes the root
  capability through, and reconstructs a handle on the other side

This lets us write one set of semantic tests (calling methods, pipelining, teardown, etc.)
and run them against every transport to verify consistent behavior.
