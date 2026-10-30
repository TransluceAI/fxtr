# Part 1 — Workstation Server (overview)

The workstation server is the long-lived local process that holds workstation state and brokers between fxtr-RPC peers (sandboxes, agent attached services) and high-level clients (browser UIs, agent MCPs). This document is the entry point for part 1's design; per-stage details live in the sibling stub plans.

See also: [`../sketch.md`](../sketch.md) for the broader workstation vision and [`./sketch.md`](./sketch.md) for the original part-1 staging notes.

## Goals

- Be a single in-process server that owns workstation-level resources: tracked `FxtrID`s, tracked services, RPC connections, and (later) sessions.
- Expose two WebSocket surfaces on the same hypercorn process:
  - **`/rpc`** — fxtr-RPC connections, opened with `open_rpc_scope`. Used by sandboxes / "attached services" / the agent MCP plugin's data-introduction hook. The workstation hosts a `WorkstationServiceProto` as the connection's main service; the peer's main is `NoOpService`.
  - **`/client`** — non-RPC client protocol used by browser debug/agent UIs. Multiplexes session subscriptions, mutations, and global queries over a single connection.
- Play well with anyio structured concurrency. Library-level API: `async with WorkstationServer(...) as ws: ...`, served by `hypercorn.asyncio.serve(app, config, shutdown_trigger=...)` running on an anyio task group.
- For phase 0, run entirely in-memory. **Restarting the server loses all workstation state.** Stage 7 layers in a minimal opt-in persistence cap.

## Non-goals (for part 1)

- Authentication / multi-tenant isolation — the workstation is a local single-user process.
- Crash recovery, transactional durability for sessions, or background indexing.
- The agent MCP plugin and the full panel-viewer client (handled in later parts).
- Reuse of the existing `projects/fxtr_workstation/ts/` panel viewer. The stage-2+ debug UI is a small standalone Vite app under `fxtr_workstation/debug_ui/` and does **not** depend on `ts/`.

## Resource model (cross-stage)

### Tracked entities
- One `FxtrID -> entity_short_id` map, persistent for the lifetime of the server: short IDs are never recycled across the run, but if an entity goes fully untracked and is later re-tracked, it gets the same short ID back.
- Short ID format: hex prefix of the ID string, default 7 chars for `d`/`n`/`c` nodes, configurable; full ID string for other node types. On collision, the prefix is extended until unique.
- Refcounted across "trackers" — an active RPC `copy_entity` registration, a pinned session entry, and any unacknowledged result/callback referencing the entity all hold one tracker. When all trackers drop, the entity becomes "untracked" and the workstation can release its scope claim.
- All tracked entities live in a single workstation-owned scope (in-memory in stages 1–5; backed by SQLite in stage 6's optional persistence cap).
- Each registration carries an *external name* — the binding evaporates the moment the registration releases, even if the entity itself remains tracked through other paths.

### Tracked services
- One `ServiceKey -> service_short_id` map, kept while the underlying `IRHandle`'s lifetime token is alive. The map entry is dropped when the handle's token ends. Short IDs are `s{N}` (autoincrement, never reused for the run).
- Each registration owns its own isolated mountable scope so registrations have independent lifetimes (per the part-0 sketch). The bridge into the workstation's scope is established at `register_service` time.
- "Registered" / "name-bound" / "id-bound" / "alive" are four overlapping states:
  - **Registered + name-bound**: original RPC connection still holds the registration; `get_service_by_external_name` works.
  - **Id-bound** (no longer name-bound): registration released, but at least one session is still tracking the service. `get_service(short_id)` works; the by-name lookup fails.
  - **Drained but alive**: nothing in the workstation tracks the service, but the underlying lease has not yet drained (something out in the wild — another RPC peer that pulled the service via `get_service` and threaded it elsewhere — is keeping it alive). The workstation surfaces this state for the debug UI but does not attempt to attribute it to a specific holder.
- We do **not** maintain a precise "who is keeping this alive" map; services flow freely through call args and aren't worth tracking that way. We do surface the binary "still alive after we let go" signal.

### RPC connections
- One record per `/rpc` WebSocket. Identified by a UUID assigned by the server. Optional human-readable `name` set via `name_connection` (last writer wins). Records carry `peer_addr`, `connected_at`, the connection's `RPCSession` / scope, and the list of registrations originating from this connection.

### Sessions (introduced in stage 3)
- UUID-keyed; carry mutable JSON state, an event-sourced log with per-session sequence numbers, a slot map for outstanding job/callback results, and a tracker subset of the workstation's tracked-entity / tracked-service sets.

## Lifetimes at a glance

```
WorkstationServer (root mountable scope)
├── tracked-entities scope (in-memory; one for whole server)
├── per-service scopes (one per registered service, isolated, mountable)
├── per-rpc-connection scopes (one per /rpc WebSocket, opened by open_rpc_scope)
└── per-session scopes (stage 3+; one per session)
```

Drain order on server shutdown: sessions first, then RPC connections (each `open_rpc_scope` body unwinds individually), then per-service scopes (some may already be gone), then the tracked-entities scope, then the root.

## Wire-protocol overview

- **`/rpc`** — JSON-framed `RPCMessage`s. Each WebSocket text frame is one JSON message; the per-frame envelope is the JSON form already produced by `fxtr2.rpc.json_serdes.encode_message`. The workstation provides a small adapter that wraps an anyio websocket as `ObjectStream[JSONTypes]`, then `wrap_json_stream` lifts it to `ObjectStream[RPCMessage]` for `open_rpc_scope`.
- **`/client`** — JSON-framed custom protocol introduced in stage 2 and grown across stages 3 and 5. Each frame is a request, response, or event with a client-allocated correlation ID.

## Stage breakdown

Each stage has its own plan, which may be a stub until we flesh out the details. Stages are roughly additive — a later stage assumes the earlier ones are in place.

1. **[stage1_core.md](./stage1_core.md)** — fxtr-RPC layer + tracked-entity / tracked-service registries. WebSocket server, `/rpc` endpoint, `WorkstationServiceProto`, short-ID allocation, release-callback lifetime model. Tests exercising two RPC connections sharing data via the workstation.
2. **[stage2_client_listing.md](./stage2_client_listing.md)** — `/client` endpoint with a minimum-viable protocol: list RPC connections, list tracked entities and services, watch for changes. A standalone `debug_ui` Vite app that renders these lists and surfaces "still alive after release" state.
3. **[stage3_sessions.md](./stage3_sessions.md)** — full session management on `/client`: subscribe, mutate (JSON Patch), event log, ack/sequence numbers, multiplexed subscriptions, session metadata search. Excludes callbacks.
4. **[stage4_debug_ui_sessions.md](./stage4_debug_ui_sessions.md)** — extend the debug UI to show session state, event logs, and the gray-out-old-events behavior described in the part-0 sketch.
5. **[stage5_callbacks.md](./stage5_callbacks.md)** — workstation-minted callback services routed back to a session's slot stream, integrated through the backend, the session interface, and the debug UI.
6. **[stage6_provenance.md](./stage6_provenance.md)** — make entity tracking provenance-aware: key records on `(data, prov)`, give each record its own scope so abandons don't bleed across shared data, rewrite the short-ID allocator with per-`data` `@N` suffixes, and add synchronous metadata-query mutation ops for entities and services.
7. **[stage7_persistence.md](./stage7_persistence.md)** — optional persistence cap: a workstation-mounted service for saving/loading entities by name into a SQLite (or git) namespace, with lazy retrieval back into the workstation.

## Out of scope for part 1

- Agent MCP integration (part 2+).
- Full UI integration with `projects/fxtr_workstation/ts/` (part 3+).
- Any cross-process workstation federation.
