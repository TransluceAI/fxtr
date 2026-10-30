# Network Transport — Scratch Notes

## Problem

Tasks in sandboxes (eryx WASM or Docker with `--network none`) have no network access. But some tasks need to call external APIs (e.g., Anthropic, OpenAI). We want to provide controlled, filtered network access where:
- The host decides which endpoints are reachable
- Secrets (API keys) are injected by the host and never visible inside the sandbox
- The task code gets a standard httpx interface

## Key decisions

### Why not eryx NetConfig + localhost proxy?
- NetConfig `allowed_hosts` does host-pattern matching but unclear if port-level filtering is supported
- `allow_localhost()` opens ALL localhost services (databases, dev servers, etc.)
- Security argument is complex: "network enabled but hopefully filtered correctly"
- Callback approach = zero network = simple security argument

### Why not an external filter program?
- Adds subprocess lifecycle management
- The filtering logic is simple (path prefix allowlist)
- aiohttp can own both filtering and proxying
- Can always add external hooks later via a protocol/callable

### Why aiohttp for the proxy?
- Already async
- Good streaming support (both request and response bodies)
- Can listen on Unix domain sockets
- Lightweight enough to embed in the host process

### Streaming design for eryx callbacks

The tricky part. HTTP streaming (SSE, chunked transfer) needs to work through eryx callbacks.

Approach: three callbacks forming a request/response lifecycle:
1. `_http_request(transport, method, path, headers_json, body) -> response_meta_json`
   - Host starts the upstream request, streams begin
   - Returns status + headers + stream_id immediately (once headers arrive)
2. `_http_read_chunk(stream_id) -> str`
   - Returns next chunk of response body (base64-encoded bytes)
   - Returns empty string when response is complete
3. `_http_close_stream(stream_id) -> None`
   - Cleanup, cancel upstream if still in progress

This maps to httpx.AsyncBaseTransport.handle_async_request() which returns a Response with a stream.

The host side needs to hold open upstream responses while the sandbox reads chunks. This means:
- A dict of {stream_id: (response, async iterator)} on the host
- _http_request starts the upstream request, stores the response iterator, returns metadata
- _http_read_chunk pulls the next chunk from the iterator
- _http_close_stream cancels/closes the response

Concurrency: eryx is single-threaded so only one stream is active at a time from the sandbox's perspective. But the host might have multiple streams open if the sandbox opens a new request before fully reading the previous one. The stream_id handles this.

Actually — can the sandbox even do concurrent requests? eryx is single-threaded Python in WASM. Even with async, it's cooperative. So realistically only one HTTP request is in flight at a time. But using stream_id is still cleaner for correctness.

### Docker UDS approach

Much simpler. aiohttp server on a UDS, one per transport. httpx natively supports UDS transports. The proxy logic is shared with the eryx path.

One socket per transport is simplest. Number of transports per task is small (1-3 typically).

Mount at `/run/fxtr/transport/{name}.sock` — direct file mount (same pattern as task.sock).

### TaskContext API

Both runtimes expose:
```python
ctx.get_transport(name) -> tuple[httpx.AsyncBaseTransport, str]  # transport, base_url
ctx.get_client(name) -> httpx.AsyncClient  # pre-configured, caller should close
```

For Docker: transport is `httpx.AsyncHTTPTransport(uds=path)`, base_url is `http://proxy`
For eryx: transport is a custom `CallbackTransport` class, base_url is `http://proxy`

The base_url doesn't matter semantically — it just needs to be a valid URL since the proxy rewrites everything. The path is what matters.

### What goes in the bundle vs. runtime config?

Runtime config (not entities, not content-addressed):
- TransportRoute: name, upstream_base_url, injected_headers, allowed_path_prefixes
- Passed to run() / run_container() at execution time
- Contains secrets, so must NOT be in the entity graph

What the task code needs to know:
- Transport names (so it can call get_transport/get_client)
- Base URL hint (optional — could be provided by the host via send/receive, or hardcoded)
- Actually, the base_url the task uses doesn't need to be the real upstream. It's `http://proxy` or similar.
  The task just does `client.post("/v1/messages", ...)` with relative paths.

### Request body forwarding

Streaming request bodies work naturally with the virtual socket approach — httpcore writes
the body chunk by chunk via `stream.write()`, which goes through `_transport_send` callbacks.
No special handling needed. Same for Docker/UDS — aiohttp streams natively.

### Header handling

The proxy should:
- Strip the Host header from the task's request (it's meaningless over UDS/callbacks)
- Set Host to match the upstream
- Inject/overwrite configured headers (Authorization, etc.)
- Forward other headers from the task (Content-Type, Accept, etc.)
- Forward response headers back to the task

Some headers should probably be stripped from responses too (e.g., Set-Cookie?), but for the spike just forward everything.

### Revised design: virtual socket instead of HTTP-aware callbacks

Original design had three HTTP-aware callbacks (_http_request, _http_read_chunk, _http_close_stream).
Revised to four byte-level callbacks (_transport_connect, _transport_send, _transport_recv, _transport_close)
that act as a virtual socket.

Why:
- Protocol agnostic — HTTP, websockets, anything works
- Streaming in both directions is natural (just read/write loops)
- Less eryx-specific code (no JSON serialization of HTTP metadata in callbacks)
- httpcore has an explicit AsyncNetworkBackend/AsyncNetworkStream extension point
- The host callback handler just bridges to a real UDS connection to the same aiohttp proxy Docker uses

The connect callback receives the transport name and opens a real UDS connection on the host side.
The send/recv/close callbacks shuttle bytes between the eryx sandbox and that real connection.
This means both Docker and eryx converge on the exact same proxy server.

### Routing by name, not by URL host

Each transport is bound at lookup time (get_transport("anthropic")), not at request time.
- Docker: each transport has its own socket path, httpx connects to the right one
- Eryx: CallbackNetworkBackend stores the transport name, ignores host/port from httpcore

The URL host in requests doesn't matter (task uses "http://proxy" or whatever). The proxy
doesn't need to inspect the Host header for routing — it already knows which route it serves
because each proxy app is bound to one route.

### WebSocket support

The raw-bytes virtual socket means websocket upgrades flow through naturally:
1. httpx_ws sends a GET with Upgrade: websocket headers
2. Bytes flow through the virtual socket / UDS to the proxy
3. Proxy forwards the upgrade to upstream
4. If upstream accepts (101), subsequent frames are raw bytes on the same connection
5. httpx_ws reads/writes frames, which become read/write calls on the virtual socket

The proxy handler needs to detect upgrade requests and switch from HTTP forwarding to
bidirectional byte copying. aiohttp has websocket support for both server and client sides.

This is an area to test carefully but the architecture supports it without changes.
