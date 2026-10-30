# Plan: Multiplexed transport socket with byte relay

## Problem

Each transport route requires its own UDS mounted into the container (N+1 total mounts for N transports). We'd like a single transport socket regardless of how many transports are configured. FD-passing via `SCM_RIGHTS` was ruled out — Apple Container's virtiofs strips ancillary data (confirmed by smoke test in `examples/container_scm_rights_smoke_test/`).

## Design overview

Replace per-transport UDS mounts with a single **transport socket** (`/run/fxtr/transport.sock`). The container opens a new connection to it for each HTTP connection it wants to make. The first thing sent is a routing header identifying the transport name. The host reads the name, routes to the right proxy, and relays bytes bidirectionally. Total mounts: 2 (task socket + transport socket), regardless of transport count.

After the routing header, the connection is a plain byte stream — httpx's HTTP/1.1 runs transparently over it. No HTTP parsing, fd-passing, or kernel mechanisms needed. Works on any platform that supports UDS mounts.

## Two stages

### Stage 1: Add multiplexer, keep TransportProxy as-is

The existing `TransportProxy` (aiohttp on UDS) continues to run, but only on host-internal sockets that are never mounted into the container. A new multiplexer accepts connections on the single transport socket, reads the routing header, connects to the right proxy's internal UDS, and relays bytes.

### Stage 2: Refactor TransportProxy to accept streams directly

Remove the aiohttp server and internal UDS from `TransportProxy`. Replace with a function that takes a stream pair and proxies the connection using h11 for HTTP parsing. The multiplexer hands streams directly to the proxy — no internal UDS hop. This also creates a shared interface that the eryx callback path could eventually use.

---

## Stage 1: Multiplexer + relay

### Routing header protocol

The container sends a routing header as the first bytes on each new connection to the transport socket. Uses the same length-prefixed framing as `protocol.py` (4-byte big-endian uint32 + UTF-8 body):

```
[4 bytes: length][JSON: {"name": "api"}]
```

The host reads this, then relays all subsequent bytes to/from the named proxy. No response to the routing header — after sending it, the container immediately starts sending HTTP.

### Container side (`fxtr_task_runner/transport.py`)

Replace `httpx.AsyncHTTPTransport(uds=path_per_transport)` with a custom `httpcore.AsyncNetworkBackend` that:

1. Opens a new connection to `/run/fxtr/transport.sock`
2. Sends the routing header (transport name)
3. Returns the socket as an `httpcore.AsyncNetworkStream`

The stream after the routing header is a normal byte stream — httpcore/httpx handle HTTP over it as usual. Each `connect_tcp` call (from httpx connection pool) opens a new connection and sends the routing header again.

```
# pseudocode — container side
class MultiplexedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, transport_name: str, socket_path: str):
        self._name = transport_name
        self._path = socket_path

    async def connect_tcp(self, host, port, **kwargs):
        sock = socket.socket(AF_UNIX, SOCK_STREAM)
        sock.connect(self._path)
        send_routing_header(sock, self._name)
        sock.setblocking(False)
        return SocketStream(sock)  # wraps in async read/write
```

### Host side (`container_runner.py`)

New components:

1. **Transport listener**: A `socket.AF_UNIX` server on a single path, mounted at `/run/fxtr/transport.sock`
2. **Accept loop**: Runs as a background anyio task. For each accepted connection: read routing header, look up proxy, connect to proxy's internal UDS, start bidirectional relay
3. **Bidirectional relay**: Two concurrent copy loops (container→proxy, proxy→container). Ends when either side closes.

```
# pseudocode — host relay
async def relay(container_sock, proxy_sock):
    async with create_task_group() as tg:
        tg.start_soon(copy_bytes, container_sock, proxy_sock)
        tg.start_soon(copy_bytes, proxy_sock, container_sock)

async def copy_bytes(src, dst):
    while chunk := await recv(src, 65536):
        await sendall(dst, chunk)
    # EOF — close write direction
```

Stop mounting per-transport sockets. Only mount the single transport socket.

### Changes summary (Stage 1)

**`fxtr_task_runner/transport.py`** (container):
- Remove path-per-transport logic
- Add `MultiplexedBackend` (httpcore `AsyncNetworkBackend`) and `SocketStream` (httpcore `AsyncNetworkStream`)
- `get_transport(name)` returns an `httpx.AsyncHTTPTransport` using `MultiplexedBackend`
- `TRANSPORT_SOCKET_PATH = "/run/fxtr/transport.sock"` (single path, replaces `TRANSPORT_SOCKET_DIR`)

**`container_runner.py`** (host):
- Create and mount single transport socket instead of per-transport sockets
- Add accept loop + relay logic (background task during container lifetime)
- Build `proxy_map: dict[str, TransportProxy]` for routing
- `TransportProxy` still started as before, just never mounted

**`fxtr_task_runner/context.py`**: No changes needed (delegates to `transport.py`).

**`protocol.py`**: No changes needed (routing header reuses existing `send_message`/`receive_message`).

**Container image**: Rebuild needed (transport.py changes).

**Tests**: Existing container tests should pass unchanged (same `ctx.get_client()` API).

### Implementation steps (Stage 1)

- [x] Write `SocketStream` (httpcore `AsyncNetworkStream` wrapping a non-blocking socket)
- [x] Write `MultiplexedBackend` (httpcore `AsyncNetworkBackend` that connects + sends routing header)
- [x] Update `get_transport()` / `get_client()` in `fxtr_task_runner/transport.py`
- [x] Add accept loop + relay logic to `container_runner.py`
- [x] Replace per-transport socket mounts with single transport socket mount
- [x] Rebuild container image
- [x] Run existing container tests
- [x] Update CODE_OVERVIEW.md

---

## Stage 2: Remove TransportProxy UDS

### Goal

`TransportProxy` currently runs an aiohttp server on a UDS. After Stage 1, nothing external connects to that UDS — only the host relay does. Remove the aiohttp server and UDS; instead have the proxy accept a stream pair directly.

### Design

Replace `TransportProxy` with a `proxy_connection()` async function:

```
async def proxy_connection(
    client_reader, client_writer,
    route: TransportRoute,
    upstream_session: aiohttp.ClientSession,
):
    # Parse incoming HTTP/1.1 request using h11
    # Check path allowlist
    # Inject headers
    # Forward to upstream via upstream_session
    # Stream response back through h11
```

Uses h11 for HTTP/1.1 parsing on the client side. Keeps aiohttp `ClientSession` for upstream requests (proven, handles connection pooling and TLS).

### Changes

**`proxy.py`**:
- Remove `TransportProxy` class (aiohttp server, UDS management)
- Add `proxy_connection()` function
- Keep `TransportRoute`, `_path_allowed`, `_merge_headers` (reused)
- Add h11 dependency

**`container_runner.py`**:
- Instead of starting `TransportProxy` instances and connecting relay to their UDS, call `proxy_connection()` directly with the relay's stream pair
- Remove proxy start/stop lifecycle

**`runner.py`** (eryx):
- Update eryx transport callback handlers to use `proxy_connection()` directly instead of connecting to a `TransportProxy` UDS
- On `_transport_connect`: create a `socket.socketpair()`. Give one end to the callbacks for blocking read/write (same shape as today's socket I/O). Give the other end (wrapped as an async stream) to `proxy_connection()` running as a background anyio task. Socketpair is the right bridge here because transport data is a byte stream with arbitrary chunk sizes, unlike the discrete-message queues used for task messaging.
- Removes the last consumer of `TransportProxy`'s UDS

**`TransportProxy`**: Fully removed — no remaining consumers of the aiohttp server or its UDS.

### Implementation steps (Stage 2)

- [x] Add h11 dependency
- [x] Write `proxy_connection()` using h11 for request parsing
- [x] Update `container_runner.py` relay to call `proxy_connection()` directly
- [x] Update eryx `runner.py` callback handlers to use `proxy_connection()` directly
- [x] Remove `TransportProxy` class entirely
- [x] Update proxy tests and transport tests
- [x] Run all tests
- [x] Update CODE_OVERVIEW.md
