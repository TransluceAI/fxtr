# Stage 5 Part 4: Cap-IR ObjectStream

## Goal

Provide a higher-level `ObjectStream[IRValue[JSONLeaf]]` interface on top of the existing JSON
transport contexts. The current transports (`JSONTransportContext`, `SimpleJSONTransportContext`)
expose a raw main-interface API where the user must set up a local `RPCHandle` and work with
`ctx.remote_main`. The ObjectStream wrapper lifts this into a bidirectional stream of `IRValue`s,
where each side can `send(value)` and `receive()` arbitrary IR values — including values that
contain `RPCHandle`s, `RPCPending`s, and other capabilities.

This abstraction is needed by the fxtr entity bridge (stage 6), which consumes a Cap-IR
ObjectStream and lifts it into an FxtrRPC ObjectStream with scope-managed entity exchange.

## Design

### What is a Cap-IR ObjectStream?

An `anyio.abc.ObjectStream[IRValue[Leaf]]` — a bidirectional async channel for IR values. This
is just the standard anyio ObjectStream ABC parameterized over `IRValue[JSONLeaf]`, so it
provides `send(item)`, `receive()`, and `aclose()`, and composes naturally with anything in the
anyio ecosystem that works with ObjectStreams.

Values sent through the stream can contain capabilities (`RPCHandle`, `RPCPending`). These
capabilities cross the wire like any other capability — handles become remote handles, pendings
become remote pendings. The stream is just the top-level delivery mechanism; once you have a
handle from a received value, you can dispatch on it normally.

### Relationship to the existing transport API

The existing transports take an `ObjectStream[JSONTypes]` (raw JSON) + a main `RPCHandle`, and
give you `ctx.remote_main`. The PROTOCOL.md already specifies how ObjectStream semantics work
on top of the main interface: both sides export a handle with a `send(value)` method, and
calling it delivers the value to the remote application.

The ObjectStream wrapper formalizes this convention. It:
1. Creates the main-interface handle (with a `send` method that enqueues received values)
2. Passes it to the transport context
3. Exposes `send()` (which calls `send` on the remote main) and `receive()` (which dequeues)

### Two entrypoints per transport

Each transport gets two ways to use it:

**Low-level (existing)**: Main-interface API for advanced use cases where you want full control
over the root capability.

```python
# Existing API — unchanged
async with JSONTransportContext(json_stream, my_main_handle) as ctx:
    remote = ctx.remote_main
    result = await remote.dispatch_and_wait(["method"], Call([arg]))
```

**High-level (new)**: ObjectStream wrapper for the common case of bidirectional value exchange.

```python
# New wrapper — yields ObjectStream[IRValue[JSONLeaf]]
async with json_transport_stream(json_stream) as stream:
    await stream.send({"key": some_handle})
    value = await stream.receive()
```

Similarly for the simplified transport:

```python
async with simple_json_transport_stream(json_stream) as stream:
    await stream.send(value)
    value = await stream.receive()
```

### Implementation: wrapper, not replacement

The ObjectStream wrappers are thin context managers that internally create the transport context
with an appropriate main interface. They do not modify the transport implementations. The
existing main-interface API remains available for users who need it.

```python
@asynccontextmanager
async def json_transport_stream(
    json_stream: ObjectStream[JSONTypes],
) -> AsyncGenerator[ObjectStream[IRValue[JSONLeaf]]]:
    receive_queue = ...  # buffer for incoming values
    main_handle = _make_stream_main_handle(receive_queue)
    async with JSONTransportContext(json_stream, main_handle) as ctx:
        yield _IRValueObjectStream(
            _send_handle=ctx.remote_main,
            _receive_queue=receive_queue,
        )
```

`_IRValueObjectStream` is a private class implementing `anyio.abc.ObjectStream[IRValue[JSONLeaf]]`.
Users interact with it via the standard ObjectStream interface.

### The main-interface convention

The main interface for ObjectStream mode is a handle with a single method:

- `send(value: IRValue[Leaf]) -> None`: Deliver a value to the stream's `receive()`.

When the application calls `stream.send(value)`, the implementation calls `send(value)` on the
remote side's main handle. This uses `submit` semantics (auto-pulled, auto-released) since the
caller does not need to pipeline through the result. This matches the convention already
described in PROTOCOL.md.

On the receiving side, the main handle's `send` method enqueues the value into an internal
buffer (e.g., an `anyio.create_memory_object_stream`). The `receive()` method dequeues from
this buffer.

### Buffering and backpressure

The internal receive buffer should have a configurable capacity (with a reasonable default,
e.g., 64 items). If the buffer is full, the remote side's `send` call will block until the
application calls `receive()`. This provides natural backpressure.

### Lifetime

The ObjectStream is alive as long as the transport context is alive. When the context exits:
- Any pending `receive()` calls raise `EndOfStream` (or similar)
- Any capabilities received from the stream follow normal teardown rules
- Capabilities that were sent but not yet consumed by the remote side are cleaned up by the
  transport

## Implementation Steps

- [x] Implement `_IRValueObjectStream` class implementing `anyio.abc.ObjectStream[IRValue[JSONLeaf]]`
- [x] Implement the `_StreamMainInterface` — a local object exposing `send` and `send_eof` methods
  that enqueue into a buffer
- [x] Implement `json_transport_stream()` context manager wrapping `JSONTransportContext`
- [x] Implement `simple_json_transport_stream()` context manager wrapping
  `SimpleJSONTransportContext`
- [x] Write tests
- [x] Update `rpc/LOCAL_CODE_OVERVIEW.md`

## Testing

### Unit tests for ObjectStream

Tests for the ObjectStream wrappers should verify:

1. **Basic send/receive**: Send a primitive value, receive it on the other side
2. **Bidirectional**: Both sides can send and receive concurrently
3. **Capabilities in values**: Send a value containing an `RPCHandle`, receive it, dispatch on
   it, verify it works
4. **Multiple values**: Send several values in sequence, receive them in order
5. **Handles survive across messages**: Send a handle in message 1, use it after receiving
   message 2
6. **Pendings in values**: Send a value containing an `RPCPending`, receive it, pull from it
7. **Backpressure**: Verify that sending blocks when the buffer is full (if feasible to test)
8. **Teardown**: When the stream context exits, pending receives raise appropriately
9. **Abort propagation**: If one side aborts, the other side's operations fail cleanly

### Integration with existing transport tests

The existing transport test suite (`test_transport.py`) is parameterized over transport
fixtures with signature `Callable[[RPCHandle], AsyncContextManager[RPCHandle]]`. The
ObjectStream tests should add a new transport fixture that uses the ObjectStream wrapper
to send a handle through:

```python
@asynccontextmanager
async def json_objectstream_transport(handle: RPCHandle[Any]) -> AsyncGenerator[RPCHandle[Any]]:
    """JSON ObjectStream transport: sends a handle through the stream layer."""
    a_to_b_send, a_to_b_recv = anyio.create_memory_object_stream[JSONTypes](64)
    b_to_a_send, b_to_a_recv = anyio.create_memory_object_stream[JSONTypes](64)

    stream_a = _MemoryObjectStreamPair(a_to_b_send, b_to_a_recv)
    stream_b = _MemoryObjectStreamPair(b_to_a_send, a_to_b_recv)

    async with json_transport_stream(stream_a) as obj_stream_a:
        async with json_transport_stream(stream_b) as obj_stream_b:
            # Send the handle from A to B
            await obj_stream_a.send(handle)
            received = await obj_stream_b.receive()
            # received should be an RPCHandle pointing to the original object
            yield received
```

This would reuse all existing transport-parameterized tests and verify that capabilities
passed through the ObjectStream layer behave identically.

A similar fixture for the simplified transport:

```python
@asynccontextmanager
async def simple_json_objectstream_transport(handle: RPCHandle[Any]) -> ...:
    # same pattern with simple_json_transport_stream
    ...
```

### Test file organization

ObjectStream-specific tests (send/receive, bidirectional, backpressure, teardown) can go in a
new `test_rpc/test_object_stream.py`. The transport-parameterized fixtures can be added to the
existing `TRANSPORTS` list in `test_transport.py`.
