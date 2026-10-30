# Stage 2 — `/client` endpoint + minimal debug UI

> **Status: working document.** Same pair-programming model as stage 1.

Goal: stand up the second WebSocket surface — `/client` — with the smallest workable read-only protocol that lets a passive client observe the workstation's state. Plus a small standalone Vite + React + TypeScript app under `fxtr_workstation/debug_ui/` that consumes it and renders three tables.

## Scope

Read-only only. Stage 3 introduces sessions and mutation; this stage is purely observability.

The three resources we expose:
- **`rpc_connections`** — live `/rpc` WebSocket connections (uuid, name, peer addr, connect time, the short-IDs of services/entities they're sourcing).
- **`services`** — tracked services (`s{N}` short id, type-id, external names, registering connection ids).
- **`entities`** — tracked entities (id-string-prefix short id, full id-string, external names, registering connection ids).

Note: client connections themselves are *not* surfaced as `rpc_connections` — they're a separate concept. The "drained but alive" state from the original sketch stays out of scope (deferred from stage 1).

## Wire protocol

Each WebSocket text frame is a single JSON object. The shape is **flat**: the discriminator (`op` for client requests, `type` for server frames) lives at the top level alongside the message-specific fields, no nested `args`. Validation is delegated to ``msgspec`` against the tagged-union schema in ``client_protocol.py`` — see that file for the canonical definitions.

### Client → server frames

```jsonc
// Subscribe to one resource. The request `id` IS the subscription id used
// in subsequent `snapshot` events.
{"id": <int>, "op": "subscribe", "resource": "rpc_connections" | "services" | "entities"}

// Cancel an active subscription.
{"id": <int>, "op": "unsubscribe", "subscription_id": <int>}
```

### Server → client frames

```jsonc
// Ack a request. For subscribe, the subscription is now active and the
// first `snapshot` will follow. For unsubscribe, the subscription is gone.
{"id": <int>, "type": "ack"}

// Error response for any request (malformed op, unknown resource, etc.).
// Does not close the connection.
{"id": <int>, "type": "error", "message": str}

// Snapshot push for an active subscription. The id matches the subscribe
// request that created it. Sent immediately after `ack`, then again on
// every mutation to the resource.
{"id": <subscription_id>, "type": "snapshot", "data": [...]}

// Server-initiated close of a subscription. Only used in stage 2 if the
// resource gets removed (no current case; reserved for stage 3+).
{"id": <subscription_id>, "type": "closed", "reason": str}
```

If the *connection* falls behind (per-connection queue overflows), the server cancels the entire client connection — see "Backpressure" below. The client sees the WS close.

### Snapshot shapes

Always full snapshots; no deltas in stage 2.

```jsonc
// rpc_connections
[
  {
    "connection_id": "<uuid>",
    "name": str | null,
    "peer_addr": str | null,
    "connected_at": "<iso8601>",
    "service_registration_short_ids": ["s0", ...],
    "entity_registration_short_ids": ["dXXXXX", ...]
  }, ...
]

// services
[
  {
    "short_id": "s0",
    "service_type_id": "<FxtrID id_string>",
    "external_names": ["echo", ...],
    "registering_connection_ids": ["<uuid>", ...]
  }, ...
]

// entities
[
  {
    "short_id": "dXXXXX",
    "fxtr_id": "<id_string>",
    "external_names": ["the-note", ...],
    "registering_connection_ids": ["<uuid>", ...]
  }, ...
]
```

The debug UI joins `registering_connection_ids` against the `rpc_connections` table to render human-readable names; the server doesn't denormalize.

## Backpressure: per-connection queue, cancel on overflow

Each client connection owns one bounded `anyio.create_memory_object_stream[ServerFrame](max_buffer_size=N)` (default `N=64` — small enough that a slow client gets caught quickly, large enough that healthy bursts don't trip it). The connection's per-connection task drains the stream into the WS.

When `notify_subscribers(resource_kind)` fans out:
- For each `ClientConnectionRecord`, look up its active subscriptions for that resource and call `send_stream.send_nowait(snapshot_frame)` for each.
- On `anyio.WouldBlock`: set `record.overflowed = True` and call `record.cancel_scope.cancel()`. Subsequent notifications check `record.overflowed` first and skip.

Cancelling the scope tears down the whole client connection — read loop, write loop, all subscriptions. The standard CM-exit cleanup removes the record from `state.client_connections` and closes the WS. The peer sees a connection drop with no in-band "you were too slow" message; that's fine for a debug UI.

## Server-side architecture

### New files

- **`fxtr_workstation/client_protocol.py`** — `msgspec.Struct` tagged unions for client→server requests (tag_field=`op`) and server→client frames (tag_field=`type`); `parse_text` / `serialize_to_text` helpers wrapping msgspec's JSON encoder/decoder. `ProtocolError` translates msgspec's `ValidationError` / `DecodeError` so the read loop has a single error type. No network IO here.
- **`fxtr_workstation/client_app.py`** — the `/client` ASGI route handler. Imports `client_protocol`. Per-connection task structure:
  ```
  client_endpoint(websocket):
      accept WS
      async with state.add_client_connection() as record:
          # record carries connection_id, send_stream, cancel_scope
          async with anyio.create_task_group() as tg:
              tg.start_soon(write_loop, websocket, record.send_receive_stream)
              tg.start_soon(read_loop, websocket, state, record)
              # blocks until both exit (cancellation, WS close, etc.)
  ```
  - `read_loop`: reads frames, dispatches to `_handle_subscribe` / `_handle_unsubscribe`, sends `ack` or `error` back via the same send queue. Subscription bookkeeping (which `subscription_id` watches which resource) lives on `record`.
  - `write_loop`: drains the send queue into `websocket.send_text(json.dumps(frame))`. Exits on stream close.
  - `_handle_subscribe`: validates resource name, registers the subscription on `record`, immediately enqueues an initial snapshot (built under `state.lock`).

### Changes to existing files

- **`state.py`**:
  - Add `ClientConnectionRecord` dataclass: `connection_id`, `peer_addr`, `connected_at`, `send_stream` (the send half of the per-connection memory stream), `cancel_scope`, `subscriptions: dict[SubscriptionId, ResourceKind]`, `overflowed: bool`.
  - Add `client_connections: dict[ClientConnId, ClientConnectionRecord]` to `WorkstationState`.
  - Add `add_client_connection(peer_addr)` async CM mirroring `add_rpc_connection` (constructs the record + memory stream + cancel scope, registers, removes on exit).
  - Add `notify_subscribers(resource_kind)` sync method on `WorkstationState`. Builds the snapshot under `self.lock` (so we get a consistent point-in-time view), then iterates `client_connections` and pushes to each subscriber for that resource. On `WouldBlock`, sets `overflowed` + cancels.
  - Snapshot builders: small sync functions that read the relevant registries and emit the JSON shapes above. Lives in `state.py` (or a sibling `state_snapshots.py` if it gets long).

- **`service_impl.py`** / **mutation sites**: inline `state.notify_subscribers("services")` / `notify_subscribers("entities")` / `notify_subscribers("rpc_connections")` after each mutation. The mutation sites:
  - `register_service` (after the sync-block append) → `services` + `rpc_connections`
  - `release_registration` (sync) → `services` + `rpc_connections`
  - `copy_entity` (after the sync-block append) → `entities` + `rpc_connections`
  - `release_entity_registration` → `entities` + `rpc_connections`
  - `add_rpc_connection` enter (after inserting record) → `rpc_connections`
  - `add_rpc_connection` exit (after removing record) → `rpc_connections`
  - `name_connection` → `rpc_connections`

- **`ws_app.py`**: add `WebSocketRoute("/client", client_endpoint)`.

### Notifier ordering

Build the snapshot AND fan out, both under `state.lock`. The fan-out is sync `send_nowait`, so the lock is held briefly. If we ever see lock contention, we can split (build under lock, fan out outside) — but that introduces "snapshot was for state at time T0, sent out as if it were at T1" and the lock-held version is simpler.

### Subscription IDs

The request `id` for a `subscribe` op IS the subscription id. Server-side bookkeeping uses that integer directly as the dict key. Unsubscribe references the same id. Reusing a still-active subscription id for a fresh subscribe → error.

## Tests

- **`tests/test_client_protocol.py`** (raw httpx-ws):
  - Connect, subscribe to each resource, verify the initial snapshot shape.
  - Subscribe to `services`, then trigger `register_service` from a separate `/rpc` connection, verify a second snapshot fires.
  - Same for `entities` (`copy_entity`) and `rpc_connections` (open/close `/rpc` peer).
  - `unsubscribe` stops further snapshots.
  - Bad request (unknown resource, malformed JSON) → `error` frame, connection stays alive.
  - Backpressure: subscribe but never read; trigger many mutations; verify the WS gets closed.
  - Two `/client` connections both subscribed → both see snapshots; tearing down one doesn't affect the other.

## Debug UI

Toolchain: **Vite + React + TypeScript**, scaffolded fresh under `fxtr_workstation/debug_ui/` (no dependency on `projects/fxtr_workstation/ts/`).

Layout:
```
fxtr_workstation/debug_ui/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── index.html
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── ws_client.ts        # bare WebSocket wrapper, subscribe/unsubscribe, snapshot dispatch
│   └── tables/             # one component per resource table
└── dist/                   # built artifacts (gitignored or tracked — TBD)
```

Static-file serving: Starlette `StaticFiles` route at `/debug` pointing at `fxtr_workstation/debug_ui/dist/`. We do not run the dev server in production; build artifacts only.

For the Python build trigger, simplest is `npm run build` from inside `debug_ui/`, run by the developer when needed. We can add a `python -m fxtr_workstation.build_debug_ui` shim later if useful, but stage 2 doesn't need one.

The UI itself: three tables (rpc_connections / services / entities), each subscribing on mount and updating on snapshot. Connection IDs in the service/entity tables are joined client-side against the rpc_connections table to render the name (or fall back to a truncated UUID).

## Implementation order

1. **`client_protocol.py`** — the message types and JSON serde, with unit tests for parse/serialize round-trips. No state, no IO.
2. **`ClientConnectionRecord` + `add_client_connection` + `notify_subscribers` + snapshot builders** in `state.py`. Unit tests for the snapshot builders.
3. **Inline notify calls** at every mutation site in `service_impl.py` / `state.py`.
4. **`/client` ASGI route** in `client_app.py` (read loop + write loop + per-connection task group).
5. **`tests/test_client_protocol.py`** — end-to-end tests over httpx-ws.
6. **CODE_OVERVIEW.md** + plan checkoff.
7. **Debug UI scaffold** — Vite + React + TS, three tables, live updates.
8. **`StaticFiles` mount** at `/debug` + a smoke test that the route serves `index.html`.
9. **CODE_OVERVIEW.md** final pass.

Steps 1-6 stand alone — the wire protocol works without the UI. Steps 7-9 layer on top.

## Out of scope for stage 2 (deferred to stage 3+)

- Sessions, mutations of any kind, the JSON-Patch state model, slot-based call results, callbacks.
- Delta-based snapshots (sketched in the original sketch). Revisit if mutation rate proves the full-snapshot approach too chatty.
- The "drained but alive" service state.
- Server-side debug endpoints beyond `/client` and `/debug`.

## Implementation steps

- [x] **`client_protocol.py`** — request/response/event message types (msgspec tagged unions) + `parse_text` / `serialize_to_text` + unit tests. `ResourceKind` literal + `RESOURCE_KINDS` set; `msgspec.Struct` types for `SubscribeRequest` / `UnsubscribeRequest` (tag_field=`op`) and `AckResponse` / `ErrorResponse` / `SnapshotEvent` / `ClosedEvent` (tag_field=`type`); flat on-wire shape (no nested ``args``). `ProtocolError` wraps msgspec's `ValidationError` / `DecodeError`. Added `msgspec>=0.21.0` as a direct dep in `pyproject.toml`. 20 unit tests in `tests/test_client_protocol_unit.py` covering happy-path subscribe/unsubscribe across all resources, all malformed-input error paths, and serialize round-trips for all four server frame types.
- [x] **`ClientConnectionRecord` + state plumbing** — record dataclass with bounded `MemoryObjectSendStream` + `CancelScope` + `subscriptions: dict[int, ResourceKind]` + `overflowed`, plus a `send_nowait(frame)` method that returns `False` and cancels the connection on overflow. `client_connections: dict[UUID, ClientConnectionRecord]` on `WorkstationState`. `add_client_connection(peer_addr, send_buffer_size=64)` async CM yielding `(record, receive_stream)`. `notify_subscribers(resource)` builds a snapshot via `_build_snapshot` (dispatching to `_build_rpc_connections_snapshot` / `_build_services_snapshot` / `_build_entities_snapshot`) and pushes `SnapshotEvent`s to matching subscribers. Added `short_id_for_service` / `short_id_for_entity` lookup helpers to `ShortIDAllocator`. New `tests/test_snapshot_builders.py` (5 tests): live-state snapshot shape, empty-state snapshots, `add_client_connection` lifecycle, overflow → cancel, notify routing.
- [x] **Inline notify calls** at all mutation sites. `release_registration` / `release_entity_registration` notify their respective resource + `rpc_connections` after the sync mutation block. `add_rpc_connection` notifies `rpc_connections` on enter (after insert, under lock) and on exit (after pop, under lock). `name_connection` notifies `rpc_connections` after setting the name, under lock. `register_service` / `copy_entity` notify their respective resource + `rpc_connections` after the sync-block appends. New integration test (`test_inline_notify_calls_fire_at_each_mutation_site`) drives every mutation site end-to-end and verifies the right snapshots fire.
- [x] **`/client` ASGI route** — `client_app.py` defines `client_endpoint` plus `_read_loop` / `_write_loop` and sync handlers `_handle_subscribe` / `_handle_unsubscribe`. Per-connection structure: accept WS → enter `state.add_client_connection` → start read + write tasks under a task group; either loop's exit cancels the TG via `tg.cancel_scope.cancel()`. Read loop catches `ProtocolError` per-frame (sends `ErrorResponse`, continues) and dispatches; handler exceptions become `ErrorResponse` keyed to the request id. Subscribe handler enqueues ack + initial snapshot in order; unsubscribe just acks. `state.build_snapshot(resource)` made public for the initial-snapshot use case. `ws_app.build_app` mounts `WebSocketRoute("/client", client_endpoint)`.
- [x] **`tests/test_client_protocol.py`** — 14 end-to-end tests over real WebSocket: subscribe-to-each-resource ack-then-snapshot (parametrized), `/rpc` connect/register/copy_entity each trigger their respective snapshots, unsubscribe stops further snapshots, malformed JSON / unknown resource / duplicate subscription id / unknown unsubscribe id all surface `ErrorResponse` with the connection staying alive, two clients independently subscribed both see snapshots, one client's disconnect doesn't affect another, backpressure → server closes the WS (driven by direct `notify_subscribers` flooding past the 64-frame buffer). Added `tests/_ws_client.py:ClientWS` + `connect_client(url)` test helper, and `WorkstationServer.client_url` property.
- [x] **Update CODE_OVERVIEW.md** for the protocol layer. New entries for `client_protocol.py` and `client_app.py`; `ws_app.py` description updated to mention both routes; tests directory listing extended with the three new test files; the `/rpc` and `/client` purposes summarized at the top.
- [x] **Debug UI scaffold** — Vite + React + TS under `fxtr_workstation/debug_ui/`. `package.json` + `tsconfig.json` + `vite.config.ts` + `index.html` + `src/{main,App}.tsx` + `src/ws_client.ts` (hand-written types matching the Python protocol; no codegen) + `src/tables/{Rpc,Services,Entities}Table.tsx`. `.gitignore` for `node_modules/` and `dist/`. README documenting the build dance. Did NOT run `npm install` here — the user runs it explicitly when they want to build the UI; the Python tests don't depend on a built UI.
- [x] **`StaticFiles` mount** at `/debug` + smoke test. New `debug_ui_dist_path: pathlib.Path | None` on `WorkstationServer` (default `None` → no `/debug` route); `WorkstationServer.bundled_debug_ui_dist_path()` returns the in-package path. `ws_app.build_app` takes the same kwarg, mounts a `Mount("/debug", StaticFiles(directory=..., html=True))` if the path exists, logs a warning and skips the mount if it doesn't. `WorkstationServer.debug_http_url` property exposes the HTTP URL for tests. `tests/test_debug_ui_static.py` covers: serves `index.html`, serves an asset, 404 when not configured, warning + 404 when configured path is missing, sanity that `bundled_debug_ui_dist_path()` resolves inside the package.
- [x] **Update CODE_OVERVIEW.md** for the debug UI.
