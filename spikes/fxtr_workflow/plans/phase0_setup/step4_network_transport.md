# Step 4: Network Transport Handle

## Goal

Add a `NetworkTransportHandle` type to the workflow system — an opaque capability token that steps can use to make network connections. This is the workflow system's mechanism for plumbing controlled network access into steps, which will become essential once steps run in sandboxes but should work identically for current non-isolated external steps.

## Design

### NetworkTransportHandle (core type)

A new "special" workflow type alongside `StepHandle` and `CacheSlot`:
- Has an `identity` property (FxtrID) used for cache key computation
- Has an `async connect() -> anyio.abc.ByteStream` method — the handle itself is a `ByteStreamConnectable`
- Can be passed as a step arg (part of `ConcreteTypes`)
- Cannot be a step result (not in `SerializableTypes`)
- No tracing/tracking needed (unlike CacheSlot, there's no replay-correctness concern)

```python
class NetworkTransportHandle(abc.ABC):
    @property
    @abc.abstractmethod
    def identity(self) -> FxtrID: ...

    @abc.abstractmethod
    async def connect(self) -> anyio.abc.ByteStream: ...

class ExternalNetworkTransportHandle(NetworkTransportHandle):
    def __init__(self, connector: ByteStreamConnectable, key_id: FxtrID): ...
    # connect() delegates to the connector
```

### Cache key integration

New marker entity `NetworkTransportHandleMarker(transport_identity: FxtrID)`, same pattern as `StepHandleMarker`. Added to `FrozenCacheKeyData` union.

In `_serialize_for_cache_key`: `NetworkTransportHandle` → `NetworkTransportHandleMarker(transport_identity=handle.identity)`.

Everything else (resolve, collect_cache_slots, replace_cache_slots, tracing, dispatch) passes transport handles through unchanged — they're not CacheSlots, not Pendings, just opaque values that happen to have identity.

### Entrypoint binding

```python
ep.bind_network_transport(connector, key_id) -> NetworkTransportHandle
```

Same pattern as `bind_external_step`: lifts key_id into scope, returns `ExternalNetworkTransportHandle`.

The `connector` is any async callable that returns an `anyio.abc.ByteStream`. The simplest connector is `lambda: anyio.connect_tcp(host, port)`. The interesting one is the TransportRoute proxy adapter (below).

### TransportRoute proxy adapter

Adapts a `TransportRoute` config into a `ByteStreamConnectable` by running `proxy_connection` in-process. Each `connect()` call:

1. Creates a `socket.socketpair()`
2. Wraps both ends as `anyio.abc.UNIXSocketStream`
3. Spawns `proxy_connection(proxy_end, route, upstream_session)` as a background task
4. Returns the client end

The proxy task reads HTTP/1.1 from the client end, validates paths against `allowed_path_prefixes`, injects headers from `injected_headers`, forwards to the upstream via aiohttp, and streams responses back. The step code on the other end just speaks plain HTTP/1.1 and never sees auth headers or the real upstream URL.

This is an async context manager that owns:
- An `aiohttp.ClientSession` for upstream requests
- An `anyio.TaskGroup` for proxy background tasks

```python
class TransportRouteConnector:
    """Async CM that adapts a TransportRoute into a ByteStreamConnectable."""
    def __init__(self, route: TransportRoute): ...
    async def __aenter__(self) -> Self: ...  # creates session + task group
    async def __aexit__(...): ...            # tears down
    async def connect(self) -> anyio.abc.ByteStream: ...  # socketpair + spawn proxy
```

**Dependency question**: `proxy_connection` and `TransportRoute` currently live in `isolation_spike.proxy`. Options:
- (a) Copy `proxy.py` into `fxtr_workflow` (~210 lines, self-contained, uses h11+aiohttp)
- (b) Import from `isolation_spike` as an optional dependency
- (c) Extract into a shared package

For this spike, **(a) copy** seems simplest — it avoids coupling the packages while we're still prototyping. We can factor it out later when the packages converge. The copy would go in `fxtr_workflow/proxy.py`.

New dependencies: `h11`, `aiohttp` (required for TransportRouteConnector but not for the core handle type).

### httpx bridge utility

A self-contained helper that wraps a `NetworkTransportHandle` into httpx types:

```python
def make_httpx_transport(handle, task_group) -> httpx.AsyncHTTPTransport
def make_httpx_client(handle, task_group, **client_kwargs) -> httpx.AsyncClient
```

Internally:
- Custom `httpcore.AsyncNetworkBackend` whose `connect_tcp` calls `handle.connect()`
- `_AnyioNetworkStream` adapter wrapping the `ByteStream` as `httpcore.AsyncNetworkStream`
- `httpcore.AsyncConnectionPool(network_backend=backend)`
- `httpx.AsyncHTTPTransport` with the pool swapped in

No base_url handling. The caller sets `base_url` on their httpx client. The reason: when running in a sandbox, the step doesn't know (and shouldn't need to know) the real upstream URL. It sends requests to whatever base URL it's told, and the proxy handles rewriting.

The `task_group` parameter is needed because httpcore's connection pool manages connections internally, but our backend's `connect_tcp` spawns proxy tasks that need a long-lived task group. Actually — looking at this more carefully, the proxy task is spawned per-connection and runs for the connection's lifetime. The connection pool controls when connections are opened/closed. So the proxy tasks need to live in a task group that outlives individual requests.

For the TransportRouteConnector case, the connector already owns a task group. For raw ByteStreamConnectable usage, the caller provides one. We could alternatively make the httpx bridge accept a `NetworkTransportHandle` that owns its own spawning (which TransportRouteConnector does).

Simplest: `make_httpx_transport` takes a task_group so it can spawn proxy tasks. If you're using TransportRouteConnector, you'd pass the connector's task group. This detail is internal plumbing; the step code just calls `handle.connect()` or uses a pre-built httpx client.

Actually, rethinking: when the handle is a raw `ExternalNetworkTransportHandle` wrapping a simple TCP connector, there's no background task at all — `connect()` just returns a stream. The task group is only needed when the connector itself spawns tasks (like TransportRouteConnector). So maybe `make_httpx_transport` doesn't need a task_group — it just calls `handle.connect()` in `connect_tcp`, and if the underlying connector needs task spawning, that's the connector's responsibility (TransportRouteConnector handles it).

Yes — this is cleaner. The httpx bridge is stateless. The connector owns its lifecycle.

```python
def make_httpx_transport(handle) -> httpx.AsyncHTTPTransport
def make_httpx_client(handle, **client_kwargs) -> httpx.AsyncClient
```

### What the step code looks like

```python
async def my_step(ctx: StepContext, transport: NetworkTransportHandle):
    # Low-level: raw byte stream
    stream = await transport.connect()
    # ... speak HTTP/1.1 over it ...

    # High-level: via httpx utility
    async with make_httpx_client(transport, base_url="http://api") as client:
        resp = await client.post("/v1/messages", json={...})
```

## Implementation steps

### Part 1: Core type and type system integration

- [x] Create `fxtr_workflow/network_transport.py` with `NetworkTransportHandle` ABC and `ExternalNetworkTransportHandle`
- [x] Add `NetworkTransportHandleMarker` entity to `entities.py`, update `FrozenCacheKeyData` union
- [x] Update `types.py`: add `NetworkTransportHandle` to `ConcreteTypes` and `PendingOrConcreteTypes`
- [x] Update `resolve.py`: handle `NetworkTransportHandle` in `_serialize_for_cache_key`
- [x] Add `bind_network_transport` to `TopLevelEntrypoint` in `entrypoint.py`
- [x] Update `__init__.py` exports
- [x] Update `CODE_OVERVIEW.md`

### Part 2: Tests for core integration

- [x] `tests/test_network_transport.py`: basic handle construction, identity, cache key computation
- [x] Test that handles pass through arg resolution unchanged
- [x] Test that handles are included in cache keys (different handle → different key)
- [x] Test that handles are excluded from cache-slot collection
- [x] End-to-end: pass transport handle to a step, verify it arrives and cache works
- [x] Test _validate_result rejects transport handles as results

### Part 3: Proxy adapter

- [x] Copy `proxy.py` from `isolation_spike` into `fxtr_workflow/proxy.py` (TransportRoute + proxy_connection)
- [x] Add h11 and aiohttp as dependencies in pyproject.toml
- [x] Create `TransportRouteConnector` in `fxtr_workflow/transport_utils.py`
- [x] Test TransportRouteConnector: connect through proxy, verify path filtering and header injection

### Part 4: httpx bridge utility

- [x] Create httpx bridge in `fxtr_workflow/transport_utils.py`: `make_httpx_transport`, `make_httpx_client`
- [x] Test: httpx client through a transport handle (direct TCP connector)
- [x] Test: httpx client through TransportRouteConnector → proxy → upstream
- [x] Update `CODE_OVERVIEW.md` (final)
