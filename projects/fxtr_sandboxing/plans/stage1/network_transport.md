# Plan: Network Transports for Sandboxed Tasks

Provide controlled, filtered network access from sandboxed tasks (eryx WASM and Docker) to external APIs. The host defines which endpoints are reachable and injects secrets; the task code gets a standard `httpx` interface and never sees credentials or unrestricted network access.

## Motivation

Tasks need to call external APIs (Anthropic, OpenAI, custom services) but run in sandboxes with no network. We want:
- **Allowlisted access only** — the task can only reach endpoints the host explicitly permits
- **Secret injection** — API keys are added by the host, invisible inside the sandbox
- **Standard interface** — task code uses `httpx.AsyncClient` with normal HTTP semantics
- **Streaming** — SSE and chunked responses (e.g., Claude API streaming) must work
- **Protocol agnostic** — the transport layer carries raw bytes, so HTTP, websockets, etc. all work without the sandbox-side plumbing knowing about them

## Design overview

```
Task code (sandbox)                         Host
─────────────────                           ────
ctx.get_client("anthropic")
  → httpx.AsyncClient                      TransportRoute("anthropic",
      │                                       upstream="https://api.anthropic.com",
      ▼ httpcore                              headers={"Authorization": "Bearer sk-..."},
  AsyncConnectionPool                         path_prefixes=["/v1/messages", "/v1/models"])
      │                                              │
      ▼ connect_tcp(host, port)                      ▼
  [virtual socket]  ─── or ───  [UDS]         aiohttp proxy server
      │                                         1. check path allowlist
      │  raw bytes (read/write)                 2. inject headers
      ▼                                         3. forward to upstream (streaming)
  eryx callbacks ──→ real UDS conn ─────────→   4. stream response back
                                                     ↑
  Docker container ──→ UDS ──────────────────────────┘
```

Both runtimes converge on the same host-side aiohttp proxy. The only difference is how the sandbox connects to it:
- **Docker**: directly via a mounted Unix domain socket
- **Eryx**: via four byte-level eryx callbacks that act as a virtual socket, with the host-side callback handler opening a real UDS connection to the same proxy

### Why raw bytes instead of HTTP-aware callbacks?

The eryx callback layer is a **virtual socket** — it knows nothing about HTTP. Four callbacks (`connect`, `send`, `recv`, `close`) carry raw bytes. This means:
- httpx/httpcore handles all HTTP semantics (framing, chunked encoding, HTTP/2, etc.)
- Streaming works naturally (just `read`/`write` loops)
- Websocket upgrades work automatically (httpx_ws over the same stream)
- The eryx-specific code is minimal and protocol-agnostic

### Why not eryx NetConfig + localhost proxy?

eryx has a `NetConfig` with host whitelisting, but `allow_localhost()` opens ALL localhost services. The callback approach gives us zero network in the sandbox — a simpler security argument.

## Configuration

### TransportRoute (runtime config, not an entity)

```python
@dataclasses.dataclass
class TransportRoute:
    name: str                                    # lookup key for task code
    upstream_base_url: str                       # e.g. "https://api.anthropic.com"
    injected_headers: dict[str, str]             # merged into every request
    allowed_path_prefixes: list[str] | None      # None = all paths allowed
```

Passed to `run()` / `run_container()` at execution time. Carries secrets, so it's **not** part of the entity graph.

```python
async with run(bundle, cache_dir, transports=[route1, route2]) as session:
    ...

async with run_container(bundle, transports=[route1, route2]) as session:
    ...
```

## Host-side proxy

An `aiohttp.web.Application` per transport, each listening on a Unix domain socket. Wraps an `aiohttp.ClientSession` for upstream requests. The handler:

1. Checks path against `route.allowed_path_prefixes` → 403 if denied
2. Builds upstream URL: `route.upstream_base_url + path_qs`
3. Merges headers: discards task's `Host`, sets `Host` to upstream hostname, applies `injected_headers` (overwrite on conflict), forwards the rest
4. Forwards the request to upstream, streaming both request and response bodies
5. Streams the response back, including status and headers

```python
# Pseudocode for the core handler:
async def proxy_handler(route, request):
    if not _path_allowed(request.path, route.allowed_path_prefixes):
        return web.Response(status=403)

    upstream_url = route.upstream_base_url + request.path_qs
    headers = _merge_headers(request.headers, route)

    async with upstream_session.request(
        request.method, upstream_url,
        headers=headers, data=request.content,
    ) as upstream_resp:
        resp = web.StreamResponse(
            status=upstream_resp.status, headers=upstream_resp.headers,
        )
        await resp.prepare(request)
        async for chunk in upstream_resp.content.iter_any():
            await resp.write(chunk)
        return resp
```

Both runtimes use the same proxy server — eryx just reaches it via a callback-bridged UDS connection instead of a direct one.

## Eryx integration: virtual socket via callbacks

### Callback layer (raw bytes, no HTTP knowledge)

Four callbacks registered on the sandbox:

```python
_transport_connect(name: str) -> str
    # Host opens a real UDS connection to the proxy for this transport name.
    # Returns conn_id.

_transport_send(conn_id: str, data_b64: str) -> str
    # Send base64-encoded bytes over the connection. Returns "".

_transport_recv(conn_id: str, max_bytes: int) -> str
    # Receive up to max_bytes. Returns base64-encoded bytes, "" = EOF.

_transport_close(conn_id: str) -> str
    # Close the connection. Returns "".
```

The host maintains a dict of `{conn_id: real_socket_connection}`. Each `_transport_connect` opens a UDS connection to the proxy server for the named transport. `_transport_send`/`_transport_recv` shuttle bytes between the callback and the real socket.

### Sandbox-side: httpcore network backend

Inside the sandbox, implement httpcore's `AsyncNetworkStream` and `AsyncNetworkBackend` interfaces. These are the explicit extension points httpcore provides for custom network transports.

```python
class CallbackNetworkStream(httpcore.AsyncNetworkStream):
    """Virtual socket backed by eryx callbacks."""
    def __init__(self, conn_id: str): ...

    async def read(self, max_bytes, timeout=None) -> bytes:
        data_b64 = await _transport_recv(self._conn_id, max_bytes)
        return base64.b64decode(data_b64) if data_b64 else b""

    async def write(self, data, timeout=None) -> None:
        await _transport_send(self._conn_id, base64.b64encode(data).decode())

    async def aclose(self) -> None:
        await _transport_close(self._conn_id)

class CallbackNetworkBackend(httpcore.AsyncNetworkBackend):
    """Routes all connections through eryx callbacks to the host proxy."""
    def __init__(self, transport_name: str):
        self._name = transport_name

    async def connect_tcp(self, host, port, **kwargs) -> CallbackNetworkStream:
        # host/port are ignored — this backend is already bound to a
        # specific transport name, resolved at lookup time.
        conn_id = await _transport_connect(self._name)
        return CallbackNetworkStream(conn_id)
```

### TaskContext (eryx)

`get_transport()` creates an httpcore pool with the callback backend, wrapped in an httpx-compatible transport:

```python
class TaskContext:
    # existing: send(), receive()

    def get_transport(self, name: str) -> tuple[httpx.AsyncBaseTransport, str]:
        backend = CallbackNetworkBackend(name)
        pool = httpcore.AsyncConnectionPool(network_backend=backend)
        # Wrap httpcore pool as httpx transport (thin adapter)
        return _HttpcoreTransport(pool), "http://proxy"

    def get_client(self, name: str) -> httpx.AsyncClient:
        transport, base_url = self.get_transport(name)
        return httpx.AsyncClient(transport=transport, base_url=base_url)
```

The transport is bound to a name at lookup time. The URL host in requests is irrelevant — routing was already decided when the task called `get_transport("anthropic")`.

### What gets injected into the sandbox

The `CallbackNetworkStream`, `CallbackNetworkBackend`, and `_HttpcoreTransport` classes are defined in `_proxy_transport.py`, which is read at runtime and injected into the sandbox alongside `_runner.py`. They only reference the four `_transport_*` callbacks (available in top-level scope) and `httpcore` (must be in `site_packages`).

Gated by `_TRANSPORTS_AVAILABLE` flag set by the host — if no transports are configured, the code isn't injected and `get_transport`/`get_client` raise an error.

## Docker integration: UDS proxy

### Container-side transport

`fxtr_task_runner` gains a `transport.py` with helpers that use httpx's built-in UDS support:

```python
TRANSPORT_SOCKET_DIR = "/run/fxtr/transport"

def get_transport(name: str) -> tuple[httpx.AsyncHTTPTransport, str]:
    socket_path = f"{TRANSPORT_SOCKET_DIR}/{name}.sock"
    return httpx.AsyncHTTPTransport(uds=socket_path), "http://proxy"

def get_client(name: str) -> httpx.AsyncClient:
    transport, base_url = get_transport(name)
    return httpx.AsyncClient(transport=transport, base_url=base_url)
```

Same as eryx: the transport is bound to a specific route by socket path. URL host is irrelevant.

### TaskContext (Docker)

```python
class TaskContext:
    # existing: send(), receive(), close()

    def get_transport(self, name: str) -> tuple[httpx.AsyncBaseTransport, str]:
        from .transport import get_transport
        return get_transport(name)

    def get_client(self, name: str) -> httpx.AsyncClient:
        from .transport import get_client
        return get_client(name)
```

### run_container() changes

`run_container()` gains a `transports` parameter. For each `TransportRoute`:
1. Create a UDS in a tmpdir, start the aiohttp proxy server on it (in the task group)
2. Mount the socket file into the container at `/run/fxtr/transport/{name}.sock` (direct file mount, same pattern as `task.sock`)

## New files

```
fxtr_sandboxing/
  proxy.py              # TransportRoute, proxy server (aiohttp app per route)
  _proxy_transport.py   # CallbackNetworkStream/Backend, _HttpcoreTransport
                        #   (injected into eryx sandbox at runtime)
fxtr_task_runner/
  fxtr_task_runner/
    transport.py        # get_transport(), get_client() for Docker containers
```

## Implementation steps

- [x] 1. Add `aiohttp` to `pyproject.toml` dependencies (fxtr_sandboxing). Add `httpx` to `fxtr_task_runner` dependencies.
- [x] 2. Write `proxy.py`: `TransportRoute` dataclass and aiohttp proxy server. One `aiohttp.web.Application` per route, listening on a UDS. The handler checks path prefixes, merges headers, forwards to upstream with streaming in both directions.
- [x] 3. Write `_proxy_transport.py`: `CallbackNetworkStream`, `CallbackNetworkBackend`, `_CallbackTransport` — the virtual socket layer injected into eryx sandboxes.
- [x] 4. Update `runner.py` (eryx): accept `transports` parameter. For each route: start the proxy server on a UDS (in a tmpdir). Register the four byte-level callbacks (`_transport_connect/send/recv/close`), with the connect handler opening a real UDS connection to the proxy. Inject `_proxy_transport.py` code into the sandbox. Set `_TRANSPORTS_AVAILABLE` flag.
- [x] 5. Update `_runner.py`: add `get_transport()` and `get_client()` to `TaskContext`, gated on `_TRANSPORTS_AVAILABLE`. These construct the callback backend bound to the requested transport name.
- [x] 6. Write `fxtr_task_runner/transport.py`: `get_transport()` and `get_client()` for Docker containers.
- [x] 7. Update `fxtr_task_runner/context.py`: add `get_transport()` and `get_client()` to Docker `TaskContext`.
- [x] 8. Update `container_runner.py`: accept `transports` parameter. For each route: start the proxy server on a UDS, mount the socket into the container.
- [x] 9. Update CODE_OVERVIEW.md with network transport concepts and new files.
- [x] 10. Write tests for `proxy.py` (19 tests): path filtering (unit), header injection/merge (unit), proxy integration (basic GET, header injection, path allowed/denied, POST body, streaming, query string, upstream error 502, httpx over UDS).
- [x] 11. Write tests for virtual socket transport (9 tests): httpx → httpcore virtual socket → proxy → upstream (basic GET, header injection, path filtering allowed/denied, POST body, streaming, multiple requests, query string, raw callback round-trip). Eryx e2e tests (8 tests, skipped until eryx is fixed): full sandbox → callback → proxy → upstream for GET, header injection, path filtering, POST, multiple requests, streaming, no-transports error, unknown-transport error.
- [x] 12. Write container transport tests in `test_container_runner.py` (3 tests: HTTP GET through proxy, header injection, path filtering). Require container runtime + rebuilt image. Updated demo task (`my_task/__init__.py`) with `http_get` command.
- [x] 13. Final CODE_OVERVIEW.md pass.

## Open questions / deferred

- **httpcore in eryx sandbox**: The sandbox-side virtual socket requires `httpcore` in `site_packages`. Since httpx depends on httpcore, tasks that already have httpx will have httpcore. But httpcore has some non-pure-Python optional dependencies (h2, socksio) — need to verify the base httpcore works in eryx.
- **TLS**: The virtual socket doesn't support `start_tls()` (raises `NotImplementedError`). This is fine because TLS termination happens at the proxy, not in the sandbox. The sandbox speaks plain HTTP to the proxy; the proxy speaks HTTPS to the upstream.
- **Websocket proxying**: The raw-bytes design supports websocket upgrades automatically — httpx_ws sends an upgrade request, the proxy forwards it, and subsequent frames flow as raw bytes. However, the proxy handler needs to detect `Upgrade` headers and switch to bidirectional frame forwarding (aiohttp supports this). Worth testing but not blocking for the initial implementation.
- **Timeout configuration**: No per-route timeout settings yet. Could add `connect_timeout` / `read_timeout` to `TransportRoute`.
- **Method filtering**: Currently only filters on path prefix. Could add allowed HTTP methods to `TransportRoute`.
- **Transport discovery**: Task code needs to know transport names in advance. Could add `list_transports()` or pass names via send/receive.
- **Connection pooling**: httpcore's `AsyncConnectionPool` will try to reuse connections. Over the virtual socket this means multiple requests reuse the same callback connection, which should work fine since the proxy is a local aiohttp server.
