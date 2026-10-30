# Plan: FD-passing for container transport sockets

## Problem

Currently, each transport route requires its own UDS mounted into the container (N+1 total mounts for N transports). This couples the container's filesystem layout to the number of transports and pushes transport topology into the mount configuration.

## Design

Replace per-transport socket mounts with a single **control socket** (`/run/fxtr/control.sock`). The container requests transport connections by name; the host sends back a connected file descriptor via `sendmsg` with `SCM_RIGHTS`. Total mounts: 2 (task socket + control socket), regardless of transport count.

### Protocol

The control socket is `AF_UNIX SOCK_STREAM`. The container opens a new connection to it for each transport request.

- **Request** (container → host): length-prefixed JSON `{"name": "api"}` (same framing as `protocol.py`)
- **Success response**: length-prefixed JSON `{"status": "ok"}` + fd as `SCM_RIGHTS` ancillary data. The fd is a socket already connected to the named proxy's UDS.
- **Error response**: length-prefixed JSON `{"status": "error", "message": "..."}`, no fd.

### Host side (`container_runner.py`)

Stop mounting per-transport sockets. Instead:
1. Create and mount a control socket at `/run/fxtr/control.sock`
2. Build a `proxy_map: dict[str, TransportProxy]` from the started proxies
3. Run a handler (in a background task) that accepts connections on the control socket, reads the transport name, connects to the appropriate proxy's UDS, and sends the connected fd back via `SCM_RIGHTS`
4. Host closes its copy of the fd after sending; the container retains its copy

### Container side (`fxtr_task_runner/transport.py`)

Replace `httpx.AsyncHTTPTransport(uds=path)` with an fd-based approach:
1. `get_transport(name)` connects to the control socket, sends the request, receives the fd via `recvmsg`
2. Wraps the connected socket as an `httpcore.AsyncNetworkStream` (async read/write via `asyncio.loop.sock_*`)
3. Wraps that in a custom `httpcore.AsyncNetworkBackend` and constructs an `httpx.AsyncHTTPTransport`

For connection pool resilience (httpx may open multiple connections), the backend's `connect_tcp` should request a fresh fd each time rather than being one-shot. This means the backend holds a reference to the control socket and the transport name.

### Shared (`protocol.py`)

Add `send_message_with_fd(sock, message, fd)` and `receive_message_with_fd(sock)` alongside the existing helpers. These use `sendmsg`/`recvmsg` with `SCM_RIGHTS` for the ancillary data.

### Public API

No changes. `TaskContext.get_transport(name)` and `TaskContext.get_client(name)` keep their existing signatures. Container task code is unaffected.

## Risk: Apple Container

`SCM_RIGHTS` passes fds at the kernel level. Apple Container uses a lightweight VM, and UDS mounts may cross a virtiofs boundary where ancillary data gets stripped. Docker on Linux is fine (same kernel, just namespaces).

**Mitigation**: Write a minimal smoke test first — a script that sends an fd over a mounted UDS in Apple Container and checks if it arrives. If it fails, we don't proceed (or scope this to Docker only).

## Implementation steps

- [ ] Smoke test: fd-passing over a mounted UDS in Apple Container
- [ ] Add `send_message_with_fd()` / `receive_message_with_fd()` to `protocol.py`
- [ ] Write `FdNetworkStream` + `FdNetworkBackend` in `fxtr_task_runner/transport.py`
- [ ] Update `get_transport()` / `get_client()` to request fd over control socket
- [ ] Update `context.py` with `CONTROL_SOCKET_PATH`
- [ ] Update `container_runner.py`: control socket, fd-sending handler, remove per-transport mounts
- [ ] Rebuild container image
- [ ] Run existing container tests
- [ ] Update CODE_OVERVIEW.md
