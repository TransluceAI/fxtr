# FD-passing transport sockets plan

## Goal

Reduce the number of sockets mounted into Docker containers from N+1 (1 task + N transports) to 2 (1 task + 1 control). The container requests transport connections by name over a control socket; the host sends back a connected file descriptor via `SCM_RIGHTS`.

## Current architecture

- Host starts one `TransportProxy` (aiohttp on UDS) per transport route
- Each proxy's socket is mounted into the container at `/run/fxtr/transport/{name}.sock`
- Container creates `httpx.AsyncHTTPTransport(uds=path)` per transport
- Transport count determines mount count: potentially unbounded

## Proposed architecture

```
Container                         Host
─────────                         ────
                                  TransportProxy("api") on UDS (not mounted)
                                  TransportProxy("db") on UDS (not mounted)

task.sock  ←──── messaging ────→  task.sock listener (unchanged)

control.sock ── "get_transport"→  control socket listener
             ←── fd via SCM_RIGHTS ──┘
                 (host connects to proxy UDS,
                  sends connected fd to container)

received fd → wrap in httpx transport → HTTP to proxy
```

Only `task.sock` and `control.sock` are mounted. Transport proxy sockets stay host-only.

## Control socket protocol

The control socket is a standard `AF_UNIX SOCK_STREAM` socket. The container connects to it for each transport request.

**Request** (container → host): length-prefixed JSON (reuse existing `protocol.py` framing):
```json
{"name": "api"}
```

**Response** (host → container): length-prefixed JSON + ancillary fd via `sendmsg`/`SCM_RIGHTS`:
```json
{"status": "ok"}
```
or on error (no fd attached):
```json
{"status": "error", "message": "unknown transport: foo"}
```

The fd in the success case is a socket already connected to the named transport's proxy UDS. The container extracts it with `recvmsg`, wraps it in `socket.fromfd()`, and uses it for HTTP.

## Host-side fd sending

```python
# pseudocode
def handle_transport_request(conn, proxy_map):
    request = receive_message(conn)  # existing protocol
    name = json.loads(request)["name"]

    proxy_sock = socket.socket(AF_UNIX, SOCK_STREAM)
    proxy_sock.connect(proxy_map[name].socket_path)

    # send response + fd
    response = json.dumps({"status": "ok"}).encode()
    header = struct.pack("!I", len(response))
    fds = array.array('i', [proxy_sock.fileno()])
    conn.sendmsg(
        [header + response],
        [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)]
    )
    proxy_sock.close()  # host closes its copy; container keeps its copy via fd
```

The host accepts connections on the control socket in a loop (or with a thread per accept), so the container can request multiple transports.

## Container-side fd receiving + httpx integration

The container calls `recvmsg` to extract the fd, then wraps the connected socket into an httpx-compatible transport. The key challenge is that httpx's `AsyncHTTPTransport(uds=...)` expects a *path*, not an already-connected socket.

Approach: write a small `httpcore.AsyncNetworkStream` wrapping the raw socket (using asyncio's `sock_recv`/`sock_sendall` for async I/O), and a one-shot `httpcore.AsyncNetworkBackend` that returns it. Then construct `httpx.AsyncHTTPTransport` using this backend. We have precedent for this pattern in the WASM `_CallbackNetworkStream`.

```python
# pseudocode — container side
class FdNetworkStream(httpcore.AsyncNetworkStream):
    """Wraps an already-connected socket fd as an async httpcore stream."""
    def __init__(self, sock):
        self._sock = sock
        self._sock.setblocking(False)

    async def read(self, max_bytes, timeout=None):
        loop = asyncio.get_event_loop()
        return await loop.sock_recv(self._sock, max_bytes)

    async def write(self, buffer, timeout=None):
        loop = asyncio.get_event_loop()
        await loop.sock_sendall(self._sock, buffer)

    async def aclose(self):
        self._sock.close()

class FdNetworkBackend(httpcore.AsyncNetworkBackend):
    """One-shot backend that returns a pre-connected stream."""
    def __init__(self, stream):
        self._stream = stream
        self._used = False

    async def connect_tcp(self, host, port, **kwargs):
        if self._used:
            raise RuntimeError("fd-based transport supports one connection")
        self._used = True
        return self._stream
```

Then `get_transport` / `get_client` would request the fd from the control socket and construct the transport from it.

**Note**: Since each fd is one connection, if httpx opens a second connection (e.g., connection pool scaling), it would fail. Two options: (a) set `max_connections=1` on the pool, or (b) have the backend request a new fd on each `connect_tcp` call (which means it needs a reference to the control socket). Option (b) is more robust and not much more complex.

## Changes by file

### `container_runner.py` (host)
- Stop mounting per-transport sockets
- Create a control socket (`control.sock`) alongside the task socket
- Mount it at `/run/fxtr/control.sock`
- After starting the container, spin up a handler that: accepts connections on the control socket, reads transport name, connects to the right proxy, sends the fd back
- Store `proxy_map: dict[str, TransportProxy]` for lookup

### `fxtr_task_runner/transport.py` (container)
- Replace path-based `get_transport()` with fd-request-based `get_transport()`
- Add `recv_fd()`, `FdNetworkStream`, `FdNetworkBackend`
- `get_transport(name)` connects to control socket, sends request, receives fd, wraps in httpx transport

### `fxtr_task_runner/context.py` (container)
- Add `CONTROL_SOCKET_PATH = "/run/fxtr/control.sock"`
- `get_transport()` / `get_client()` pass through to updated `transport.py`

### `fxtr_task_runner/protocol.py` (shared)
- Add `send_message_with_fd()` and `receive_message_with_fd()` helpers using `sendmsg`/`recvmsg`
- Keep existing `send_message`/`receive_message` unchanged

### Tests
- Existing container tests should pass unchanged (same `ctx.get_client()` API)
- Could add a unit test for `send_fd` / `recv_fd` round-trip without a container

### Container image
- Rebuild needed since `fxtr_task_runner` changes (but the task code and API are unchanged)

## Apple Container risk

`SCM_RIGHTS` is a kernel mechanism. Apple Container runs a lightweight VM — if the UDS mount crosses the VM boundary via virtiofs, ancillary data may be stripped. Docker on Linux is fine (just namespaces, same kernel).

**Mitigation**: Before implementing the full feature, run a quick smoke test in Apple Container — a minimal script that passes an fd over a mounted UDS and checks if it arrives. If it fails, we fall back to the current approach (or explore alternatives like a relay proxy on the control socket).

## Implementation steps

- [ ] Smoke test: fd-passing over mounted UDS in Apple Container (if Apple Container is in scope)
- [ ] Add `send_message_with_fd()` / `receive_message_with_fd()` to `protocol.py`
- [ ] Write `FdNetworkStream` and `FdNetworkBackend` in `fxtr_task_runner/transport.py`
- [ ] Update `fxtr_task_runner/transport.py` `get_transport()` to request fd over control socket
- [ ] Update `fxtr_task_runner/context.py` with control socket path
- [ ] Update `container_runner.py`: control socket setup, fd-sending handler, remove per-transport mounts
- [ ] Rebuild container image
- [ ] Run existing container tests
- [ ] Update CODE_OVERVIEW.md
