# Multiplexed transport plan — scratch notes

## What we learned from the fd-passing spike

- SCM_RIGHTS doesn't work over Apple Container's UDS mounts (virtiofs strips ancillary data)
- Smoke test confirmed: connection works, data works, but fd ancillary data is silently dropped
- Need a userspace-level approach instead of a kernel mechanism

## Revised approach: multiplexed byte relay

Instead of passing fds, use the single UDS as a raw byte pipe. The container opens a new connection to the transport socket for each HTTP connection it wants to make. The first thing it sends is a routing header (transport name). The host reads the name, connects to the right proxy, and becomes a bidirectional byte relay.

### Why this works where fd-passing didn't

Everything stays at the byte level — no kernel ancillary data. The UDS is just a pipe. The routing happens in Python on the host. Works on any platform that supports UDS mounts (which we already know works on Apple Container).

### How httpx integration works on the container side

httpx's `AsyncHTTPTransport(uds=...)` does: for each connection in the pool, `socket() → connect(path)`. We replace this with a custom `httpcore.AsyncNetworkBackend` where `connect_tcp` does: `socket() → connect(transport_socket_path) → send(routing_header)`. After the routing header, the connection is a normal byte stream — httpx's HTTP/1.1 implementation works transparently over it.

This is actually simpler than the fd-passing approach because we don't need `FdNetworkStream` or `recvmsg` — it's just a regular socket with a prefix.

### Routing header framing

Needs to be simple and unambiguous. Options:
- Length-prefixed (like protocol.py): 4-byte uint32 + JSON
- Newline-delimited: `api\n`
- Fixed-size with padding

Newline-delimited is simplest and there's no ambiguity since transport names are simple identifiers. But length-prefixed is more consistent with the rest of the codebase. Let's go with length-prefixed JSON for consistency: `{"name": "api"}`.

### Stage 1 vs Stage 2

**Stage 1**: Add the multiplexer + relay. TransportProxy stays as-is on its own UDS. The relay connects to the proxy UDS and forwards bytes. This changes: container_runner.py (host multiplexer), transport.py (container routing header), and removes per-transport mounts.

**Stage 2**: Refactor TransportProxy to accept stream pairs directly instead of serving on a UDS. The multiplexer hands streams to the proxy instead of connecting to a UDS. This removes the internal UDS entirely. Also unifies with the eryx path potentially.

### Stage 1 host-side relay

The host needs to:
1. Listen on a single "transport" UDS
2. Accept connections in a loop
3. For each connection: read routing header → look up proxy → connect to proxy UDS → relay bytes bidirectionally
4. Each relay runs as a background task

The relay is a standard bidirectional byte forwarder:
```python
async def relay(a, b):
    async with anyio.create_task_group() as tg:
        tg.start_soon(copy, a, b)
        tg.start_soon(copy, b, a)

async def copy(src, dst):
    while True:
        data = await src.receive(4096)
        if not data:
            break
        await dst.send(data)
```

### Stage 2: removing TransportProxy UDS

Current TransportProxy._handle() does:
1. Check path allowlist
2. Build upstream headers (merge + inject)
3. Make upstream request with aiohttp ClientSession
4. Stream response back

To remove the UDS, we'd change TransportProxy from "aiohttp server" to "function that takes a byte stream." We'd need to parse the incoming HTTP request ourselves (since aiohttp's server was doing that). h11 is the natural choice — it's a synchronous HTTP/1.1 state machine, ~30 lines to wire up.

New interface would be something like:
```python
async def proxy_connection(reader, writer, route, upstream_session):
    # Use h11 to parse request from reader
    # Apply path filtering + header injection
    # Forward to upstream via aiohttp ClientSession
    # Stream response back through writer via h11
```

This could then be called from:
- Docker multiplexer (reader/writer from the relay's proxy-side socket)
- Eryx callbacks (reader/writer wrapping the callback byte channels)

### Questions

Q: Should the transport socket be the same socket as the task socket?
A: Probably not — the task socket has its own length-prefixed protocol and is used for application messages. Mixing transport traffic into it would complicate both sides. Two sockets (task + transport) is clean.

Q: What about the eryx side? Does this plan change anything for WASM tasks?
A: Stage 1 doesn't touch eryx at all. Stage 2 creates the shared proxy interface that eryx could eventually use, but that's a follow-up. The eryx callback mechanism is more complex (crossing WASM boundary) so it's best left separate for now.

Q: Connection lifecycle — when does the relay end?
A: When either side closes. The relay's `copy` loop sees EOF (empty read) and exits. The task group cancels the other direction. The proxy UDS connection closes, which the TransportProxy handles normally.
