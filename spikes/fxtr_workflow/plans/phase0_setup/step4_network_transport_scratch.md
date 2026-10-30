# Step 4: Network Transport Handle — Scratch Notes

## Design decisions and rationale

### Where does it fit in the type system?

NetworkTransportHandle is a new "special" workflow type, like StepHandle and CacheSlot:
- ConcreteTypes: yes (can be passed as step args)
- PendingOrConcreteTypes: yes (same)
- SerializableTypes: no (it's a live capability, not data)

It cannot be a step result (validated by `_validate_result`).

### Cache key behavior

Like StepHandle, the handle's identity (a FxtrID) is included in cache keys.
If you swap out which API transport you pass to a step, that changes the cache key.

New marker entity: `NetworkTransportHandleMarker(transport_identity: FxtrID)`.
Same pattern as `StepHandleMarker`.

### Not tracked like CacheSlot

CacheSlots get tracing proxies (TracingCacheSlot) because their *contents* affect
replay correctness. NetworkTransportHandle is different — it's an opaque capability
whose identity is already in the cache key. There's no "has the network changed?"
replay check. The handle passes straight through arg resolution, tracing, etc.

In resolve.py:
- `_resolve_value`: pass through (already does this for anything not Pending/list/dict)
- `_serialize_for_cache_key`: NetworkTransportHandle -> NetworkTransportHandleMarker
- `collect_cache_slots` / `replace_cache_slots`: skip (not a CacheSlot)

In step_context.py:
- `_unwrap_tracing`: pass through (not a TracingCacheSlot)

No special treatment needed in entrypoint dispatch logic.

### The proxy layer (TransportRoute -> ByteStreamConnectable)

The key insight: the ByteStreamConnectable produced from a TransportRoute IS the
proxy. When connect() is called:

1. Create a socket pair (or anyio memory stream pair?)
2. Spawn proxy_connection() as a background task on one end
3. Return the other end to the caller

proxy_connection() from the isolation spike already takes an anyio.abc.ByteStream
(the client side), a TransportRoute, and an upstream aiohttp.ClientSession. It
handles HTTP/1.1 parsing, path filtering, header injection, and upstream forwarding.

The step code sends raw HTTP/1.1 down its end of the stream. The proxy intercepts,
validates, and forwards. The step never sees the real upstream URL or auth headers.

This is exactly the same as the Unix socket path in the isolated case, just wired
up in-process. Step code behaves identically in both environments.

### Socket pair vs memory stream pair

The isolation spike tests use `socket.socketpair()` + `anyio.abc.UNIXSocketStream.from_socket()`.
This works but involves real file descriptors. anyio also has `create_memory_object_stream`
but that's for typed objects, not bytes.

Actually, anyio doesn't have a pure in-memory byte stream pair. The socketpair approach
is the standard way. It's fine — the FDs are cheap and the kernel handles buffering.

### httpx bridge utility

Pattern from the isolation spike tests (_ProxyBackend + _AnyioNetworkStream):
1. Custom httpcore.AsyncNetworkBackend whose connect_tcp calls handle.connect()
2. Wrap the resulting ByteStream in an httpcore.AsyncNetworkStream adapter
3. Create httpcore.AsyncConnectionPool with this backend
4. Create httpx.AsyncHTTPTransport with the pool swapped in

The test code already does exactly this. We can extract it into a clean utility.

No base_url handling — the caller sets that on their httpx client. The transport
just provides the pipe.

### What needs aiohttp?

proxy_connection() uses aiohttp.ClientSession for upstream requests. This means:
- The TransportRoute adapter needs to manage an aiohttp session
- This session needs a lifecycle (create/close)

Options:
1. Accept an aiohttp.ClientSession from the caller (flexible but leaks the dep)
2. Create one internally per TransportRoute adapter (simpler API, owns lifecycle)
3. Accept the session at bind time in the entrypoint

Since the proxy is created at bind_network_transport time and needs to live for
the duration of the workflow, option 2 seems right. The adapter creates the session
and the caller (or the entrypoint) is responsible for closing it.

Actually — the entrypoint could manage the lifecycle. But NetworkTransportHandle
doesn't require a TransportRoute; it accepts any ByteStreamConnectable. The
TransportRoute adapter is a separate utility. So the utility itself should manage
the aiohttp session lifecycle (e.g., as an async context manager).

### Dependencies

Current fxtr_workflow deps: anyio, fxtr2
New deps needed:
- h11 (for proxy HTTP parsing) — only if TransportRoute adapter lives here
- aiohttp (for upstream requests) — only if TransportRoute adapter lives here
- httpcore + httpx — only for the httpx bridge utility

These should probably be optional dependencies. The core NetworkTransportHandle
type and its cache key integration don't need any of them.

Hmm, but httpx is already in the dev dependency-group. And h11/aiohttp would be
new. Maybe the TransportRoute adapter should live in the isolation_spike package
(or a shared one) since that's where proxy.py already lives?

Actually, I think the cleanest approach:
- Core type (NetworkTransportHandle, marker entity, type system integration) lives in fxtr_workflow
- The proxy/TransportRoute adapter can import from isolation_spike.proxy (it already has h11+aiohttp)
- The httpx bridge is a small self-contained utility in fxtr_workflow (httpx is already a dev dep)

Wait, but fxtr_workflow doesn't depend on isolation_spike. And we don't want to
couple them. Better to either:
(a) Copy the proxy logic into fxtr_workflow (duplicating proxy.py), or
(b) Extract proxy.py into a shared location, or
(c) Have the TransportRoute adapter live somewhere that can import both

For now, let's just put the TransportRoute adapter in fxtr_workflow with its own
copy of the necessary proxy logic (or import from isolation_spike as a dev/optional
dep). We can refactor later when the packages converge.

Actually, simplest: the TransportRoute adapter is a utility in fxtr_workflow that
imports proxy_connection from isolation_spike. We add isolation_spike as an optional
dependency. If you don't use TransportRoute, you don't need isolation_spike.

### Task group concern

proxy_connection() needs to run as a background task. Each connect() call spawns
a new proxy task. This needs a task group.

Options:
1. The ByteStreamConnectable carries a task group reference
2. Use anyio.create_task_group internally per connection (but proxy needs to outlive
   the connect call)
3. The entrypoint's task group is available and could be used

Actually, looking at the test code: `task_group.start_soon(proxy_connection, ...)`.
The proxy task runs for the lifetime of the connection. It needs to be spawned into
a long-lived task group.

The NetworkTransportHandle could accept a task group at construction time (from
the entrypoint). Or the TransportRoute adapter could own one.

Since bind_network_transport happens inside the entrypoint's context (which owns
a task group), we could pass the task group to the connector. But that couples the
connector interface to anyio task groups...

Alternatively: the connector doesn't spawn anything. It just returns a ByteStream
that, when used, proxies internally. But proxy_connection is fundamentally a
concurrent task — it reads from the client stream and writes to upstream.

I think the cleanest is: the TransportRoute -> ByteStreamConnectable adapter is
itself an async context manager that owns a task group. You enter it, it starts
the task group, and connect() spawns proxy tasks into it. When you exit, all
proxy connections are torn down.

For the entrypoint binding, the handle just wraps whatever ByteStreamConnectable
the caller provides. The TransportRoute adapter manages its own resources.

### Summary of new files/changes

New files:
- `fxtr_workflow/network_transport.py`: NetworkTransportHandle ABC, ExternalNetworkTransportHandle
- `fxtr_workflow/transport_utils.py`: httpx bridge utility, TransportRoute adapter

Modified files:
- `fxtr_workflow/types.py`: add NetworkTransportHandle to ConcreteTypes, PendingOrConcreteTypes
- `fxtr_workflow/entities.py`: add NetworkTransportHandleMarker, update FrozenCacheKeyData
- `fxtr_workflow/resolve.py`: handle NetworkTransportHandle in _serialize_for_cache_key
- `fxtr_workflow/entrypoint.py`: add bind_network_transport method
- `fxtr_workflow/__init__.py`: export new types
- `fxtr_workflow/CODE_OVERVIEW.md`: update

New test files:
- `tests/test_network_transport.py`: unit tests for the type and cache key integration
- `tests/test_transport_utils.py`: integration tests for httpx bridge and TransportRoute adapter

### Open question: where to put the proxy

Since proxy_connection already exists in isolation_spike, and we don't want to
copy it, maybe the right move is:
- For now, put the TransportRoute adapter as a utility that requires the user to
  provide both a proxy function and a session, rather than importing proxy_connection
- Or, just re-export / depend on isolation_spike as optional
- Or, actually just copy proxy.py — it's 210 lines and self-contained

I'll flag this in the plan as a decision point.
