# fxtr_workstation

Workstation system for fxtr. **Status:** under active construction
following [`plans/phase0/part1_ws_server/`](plans/phase0/part1_ws_server/).

The workstation server is a long-lived local process that brokers
between fxtr-RPC peers (sandboxes, agent attached services) and
high-level clients (browser UIs, agent MCPs). Two WebSocket surfaces
on the same hypercorn process:

- **`/rpc`** — fxtr-RPC connections via `open_rpc_scope`. Used by
  sandboxes / agent attached services / anything that wants to
  register services or copy entities into the workstation.
- **`/client`** — observer + session protocol. Clients subscribe to
  one of four resources (`rpc_connections`, `services`, `entities`,
  `sessions`); the server pushes a `resource_snapshot` on subscribe
  followed by per-mutation `resource_changes` deltas. Sessions are
  the unit of stateful interaction: a client creates a session, then
  drives it with JSON-Patch mutations carrying typed ops (track /
  untrack, load / silent-load entity, invoke, create / close
  callback, get-metadata, preserve / unpreserve events, close).
  See [`plans/phase0/part1_ws_server/overview.md`](plans/phase0/part1_ws_server/overview.md)
  for the protocol design.
- **`/debug`** (optional) — static-file mount for a small Vite + React
  debug UI that consumes `/client`. Only mounted when
  `WorkstationServer(debug_ui_dist_path=...)` is set and the path
  exists.

## Directory Structure

```
fxtr_workstation/
├── pyproject.toml        # Package config (hypercorn + starlette + msgspec + fxtr2 + fxtr_sandboxing)
├── fxtr_workstation/     # Python package — see "Module Breakdown" below
│   ├── debug_ui/         # Vite + React + TS standalone debug viewer
│   │   ├── package.json  # pnpm — `pnpm install && pnpm build` produces dist/
│   │   ├── src/          # ws_client.ts + App.tsx + tables/ (rpc-connections, services, entities, sessions, JsonTree)
│   │   └── dist/         # build artifacts (gitignored); served at /debug
│   └── dynamic_dispatch/ # Runtime-loaded frontend renderer system: typed service protocol (resolve + subscribe), entity types (RendererEntity, ReloadEverythingNotification), and BasicDynamicDispatchImpl with hot-reload broadcast. See "Dynamic dispatch" below.
├── examples/
│   ├── debug_ui_demo.py             # rpc/services/entities demo
│   └── debug_ui_sessions_demo.py    # session-protocol demo
├── tests/                # see test file names for coverage scope
├── plans/
│   └── phase0/
│       ├── sketch.md
│       └── part1_ws_server/
│           ├── overview.md
│           ├── stage1_core.md
│           ├── stage2_client_listing.md
│           ├── stage3_sessions.md
│           ├── stage4_debug_ui_sessions.md
│           ├── stage5_callbacks.md
│           ├── stage6_provenance.md
│           ├── stage7_persistence.md
│           └── stage8_session_caching.md
└── frontend_lib/         # @fxtr-ui/* TS packages — slot machinery + workstation shell
    └── packages/
        ├── fxtr-ui-core/
        ├── fxtr-ui-connector-workstation/
        ├── fxtr-ui-react/
        ├── fxtr-ui-react-dispatch/
        ├── fxtr-ui-react-dispatch-dynamic-shim/   # installFxtrExternals: import-map shim for runtime renderer modules
        ├── fxtr-ui-state-util/
        ├── fxtr-ui-workstation-dispatch/ # Canonical dispatch keys + fallbacks + RefLink + JsonRefTree + MetadataViews (externalized)
        ├── fxtr-ui-workstation/         # dockview shell + debug panels (workstation, session, dispatcher) + ShellRefLinkImpl
        ├── fxtr-ui-reports/             # Figure dispatch key + RootReport/SubReport renderers
        ├── fxtr-ui-demo/
        ├── fxtr-ui-dispatch-demo/
        ├── fxtr-ui-dynamic-dispatch-demo/   # dual-route: legacy DispatchGrid + workstation shell
        ├── fxtr-ui-workstation-demo/
        └── fxtr-ui-published-app/       # demo-agnostic viewer (HttpConnector + PanelViewer) for any `fxtr_nix serve` flake; served via --web-dist
```

An older panel-viewer prototype used to live under `ts/` (built on
`@fxtr/core` + `@fxtr/react` + `@fxtr/react-dispatch`, never wired into
the workstation server). It has been archived to
[`spikes/old_typescript/`](../../spikes/old_typescript/CODE_OVERVIEW.md);
the current debug UI lives under `debug_ui/`.

`frontend_lib/` implements the slot machinery on top of `/client`
and the dockview-based workstation shell on top of that —
see `frontend_lib/CODE_OVERVIEW.md` for the current surface and
`plans/phase0/part3_frontend_ui/` for design notes and TODOs.
The workstation shell adds an open-panels sidebar, dockview
panel grid, ref dispatch, breadcrumb / history navigation,
metadata views, a workstation-level debug panel, per-session
debug panels with force-terminate, and a connection-status
header. Live integration tests in
`frontend_lib/packages/fxtr-ui-connector-workstation/tests/integration.test.ts`
exercise the slot path against the workstation server through the
`_demo_workstation.py` script in that directory.

## Module Breakdown

### `server.py` — `WorkstationServer`

Async context manager (subclass of `anyio.AsyncContextManagerMixin`)
that's the public entrypoint. Reserves an ephemeral port, captures a
parent scope (or accepts an explicit `parent_scope=...`), opens an
`isolated=True, mountable=True` `root_scope` that includes the
parent (and each configured persistence namespace's `ns.scope`),
constructs a `WorkstationState`, runs `hypercorn.asyncio.serve` on
its own task group, polls TCP-connect until listening, and triggers
a `shutdown_trigger` event on exit.

Configuration options:

- `debug_ui_dist_path` — wires the static debug UI into the route
  table.
- `persistence_namespaces=[PersistenceNamespaceConfig, ...]` — each
  entry is an `(external_name, FxtrNamespace, mode)` triple. The
  server validates uniqueness + mode at construction, includes each
  namespace's scope in `root_scope`, and spawns a
  `persistence_cap_worker` per config (via
  `background_tg.start(...)`) so each cap is fully ready before
  `yield self`.
- `external_services=[ExternalServiceConfig, ...]` — each entry
  pairs an `external_name` with a user-supplied `(ServiceRef,
  FxtrScope)` pair. The server bridges each ref into `root_scope`
  via `bridge_service_from_scope` and runs `attach_external_service`
  per config (no per-config worker — no per-config scope to own).

External-name uniqueness is validated across both lists by
`_validate_system_service_configs`. On shutdown the server walks
every service record and releases each `SystemSource`-sourced
registration so the cap workers (and external services' standard
`service_record_worker`s) can wake from their `shutdown_event` park
and let `background_tg.__aexit__` complete; `root_scope.aclose` then
releases the bridges to the user-supplied scopes.

Exposes `bound_url`, `rpc_url`, `client_url`, `debug_http_url`,
`state`, and the `bundled_debug_ui_dist_path()` helper.

### `state.py` — registries + lifetime model

In-memory registries plus the lifetime model for tracked services,
entities, and sessions. The single hottest file in the package; see
[`plans/phase0/part1_ws_server/stage3_sessions.md`](plans/phase0/part1_ws_server/stage3_sessions.md)
"Invariants and primitives" for the session state machine in full
detail.

**`WorkstationState`** fields:

- `root_scope` — workstation's primary scope; per-record entity
  scopes and per-service-record scopes are children of this.
- `background_tg` — long-lived task group for per-record workers
  and result-delivery workers.
- `rpc_connections: dict[UUID, RPCConnectionRecord]`.
- `service_records: dict[ServiceKey, TrackedServiceRecord]`.
- `service_external_names: dict[str, Registration]` — exclusive;
  `register_service` rejects duplicates.
- `entity_records: dict[ProvenancedKey, TrackedEntityRecord]` —
  keyed on `ProvenancedKey = (data_id, prov_parent)` so layered
  FxtrIDs that differ only in provenance get distinct records.
- `entity_external_names: dict[str, EntityRegistration]` — separate
  namespace, also exclusive.
- `sessions: dict[UUID, SessionRecord]`.
- `short_ids: ShortIDAllocator`.

**Tracked-record lifetimes.**

- **`RPCConnectionRecord`** carries `connection_id`, `peer_addr`,
  `connected_at`, `scope`, `name`, and the registrations sourced by
  this connection. `add_rpc_connection(peer_addr)` is an async CM:
  opens an `isolated=True, mountable=True` per-connection scope with
  `include=[root_scope]`, constructs the record, and on exit
  defensively releases any registrations (service + entity) still
  sourced from the connection before removing the record.
- **`TrackedServiceRecord`** + **`Registration`** + the
  **`service_record_worker`** task — one record per `ServiceKey`.
  The worker opens an `isolated=True, mountable=True`
  `service_scope`, bridges the source service into it
  (`bridge_service_from_scope`), sets `bridge_ready`, and parks on
  `shutdown_event`. When the last registration drops,
  `release_registration` sets `shutdown_event` and the worker drains
  the scope. **Important caveat**: the bridge pins its source scope
  (the dispatch `call_scope` of `register_service`), which creates a
  structural cycle if the holder of the chain-bridged `release_cb_ref`
  also owns the registered ref — see `register_service`'s docstring
  for the two structurally safe usage patterns (explicit `release()`,
  or hold the release callback in a child scope of the registered
  ref's scope).
- **`TrackedEntityRecord`** + **`EntityRegistration`** — mirror of
  the service shapes for entities. Each record owns its own isolated,
  non-mountable `FxtrScope` (child of `root_scope`) holding the
  layered FxtrID. On last-release `_maybe_release_entity_record`
  calls `record.scope.force_close()`. The v2 arena's per-scope
  refcounting keeps overlapping data subgraphs alive across distinct
  records, so releasing one `(data, prov_X)` record never disturbs a
  coexisting `(data, prov_Y)` record. Consumers that retrieved the
  FxtrID via `get_entity` keep working past last-release because the
  RPC layer bridges each returned entity's lifetime token across the
  wire.

**Registration sources.** `Registration.source` is a union of
`RPCConnectionRecord | SystemSource`. `SystemSource(kind, metadata)`
covers workstation-internal mounts: persistence caps
(`kind="persistence"`), external services (`kind="external"`), etc.
The helper `describe_registration_source` produces a uniform
error-message fragment across both source kinds.
`EntityRegistration.source` is RPC-only.

**Entity-record helpers** (all sync; must run inside a
`mutation_context`):

- `_open_record_scope(state, fxtr_id)` — mints the isolated scope
  with `include=[root_scope]`.
- `_install_entity_record(state, fxtr_id, record_scope)` —
  allocates the short ID, touches `entities`, and inserts the record
  into `entity_records` keyed on `provenanced_key_for(fxtr_id)`.
- `_ensure_entity_record_via_copy(state, fxtr_id, source_scope)` —
  async; opens scope + `copy_from_scope` + race-checked install.
  Used for inbound RPC `copy_entity` and invoke / callback result
  delivery.
- `_ensure_entity_record_via_track(state, fxtr_id, source_scope)` —
  sync; opens scope + `track_from_scope` + install. Used for
  same-store transfers (load-result children where `load_cbor` has
  already brought the data into a transient `load_scope`).

**Release primitives.** `release_registration(state, registration)`
/ `release_entity_registration(state, entity_registration)` are
idempotent sync functions that drop a registration from all
bookkeeping. The service version also sets the corresponding
record's `shutdown_event` if no registrations remain. Safe to call
from sync end-callbacks or async dispatch. They open their own
`mutation_context` if none is active and join the parent's context
if one is.

**Sessions.** `SessionRecord` carries:

- Identity: `session_id`, `metadata`, `created_at`.
- Authoritative state: `state` (the JSON-Patch–mutated user state),
  `last_state_seq`, `last_state_ack_seq`, `slot_counter`,
  `tracked_entities: set[ProvenancedKey]`, `tracked_services:
  set[ServiceKey]`. `StateUpdateEvent`s are derived from these
  fields; they're *not* persisted in `event_log`.
- Event log: `event_log: list[NonStateSessionEvent]` (contiguous
  unacked-suffix; non-state events only), `event_seq` (per-session
  monotonic counter for any event), `event_refs[seq] = (entity_set,
  service_set)` (per-retained-event ref sidecar for retention
  decrement), `preserved_events: dict[int, NonStateSessionEvent]`
  (sidecar holding events explicitly held past the ack horizon by a
  `PreserveEventsOp`).
- Refcounts: `entity_refcounts: dict[ProvenancedKey, int]`,
  `service_refcounts: dict[ServiceKey, int]` — sum of explicit pins
  + retained event refs + in-flight delivery contributions. The
  invariant is `session ∈ TrackedXRecord.tracking_sessions ⟺
  session.x_refcounts[key] > 0`.
- In-flight slots: `open_callbacks: dict[int, CallbackRecord]`,
  `open_invokes: dict[int, OpenInvokeInfo]`, `open_loads: dict[int,
  ProvenancedKey]` — populated when a slot's work starts and
  removed at the slot's terminal event.

**Session tracker channel.** Tracking is *separate* from RPC
`Registration`s. `TrackedServiceRecord.tracking_sessions: dict[UUID,
SessionRecord]` and the entity equivalent cross-reference each
session's `tracked_*` set. A tracked record stays alive while
*either* its `registrations` list or its `tracking_sessions` dict
is non-empty.

**RPC lifetime under session trackers.** A session that tracks a
service via `TrackServiceOp` keeps the registering RPC connection
alive — the bridge in `service_scope` pins the dispatch scope of
the original `register_service` call, so a *graceful* RPC drain
blocks until the session releases. This is by design: the session is
the responsible party for cleanup. Entity registrations are
independent (entities are copied into their per-record scopes, no
pin). A *cancelled / crashed* RPC connection forces tear-down
regardless.

**The mutation pipeline.** `apply_mutation` is the sync core
processing a `_PendingMutation` (queued by `_handle_mutate_session`
into the session's per-session `_PendingMutation` queue and drained
by `session_worker`). Steps:

1. Stale-ack check (`stale_ack` rejection if behind).
2. Op-validation dry run with working sets — preserves /
   unpreserves / tracks / untracks all simulated; slot
   monotonicity enforced via a single running `max_slot_claimed`
   counter; invoke-arg `unescape` walk; callback `unescape` resolves
   slots against `pending_creates ∪ open_callbacks`. Any failure →
   `validation_failed`.
3. **Async pre-commit** — only path that awaits. For each
   `CreateCallbackOp` in the batch, spawns `callback_record_worker`
   via `background_tg.start(...)` so each callback is fully ready
   (record + bridge) before commit. Bridge failure on a
   workstation-internal service rejects the whole mutation as
   `callback_setup_failed`. See
   [`plans/phase0/part1_ws_server/stage5_extra1_async_mutation.md`](plans/phase0/part1_ws_server/stage5_extra1_async_mutation.md)
   for the async-precommit design.
4. Sync commit phase, in order: apply JSON Patch → tracker ops →
   metadata ops resolve (sync) → preserve / unpreserve sidecar
   moves → retention prune (decref `event_refs[seq]` for everything
   that drops past `last_state.ack_seq`, then drop the prefix from
   `event_log`) → bump `slot_counter` → spawn `load_worker` /
   `invoke_worker` / `direct_load_worker` for async result delivery
   → attach `CallbackRecord`s to `open_callbacks` (incref open-
   callback service contribution) → close-drain (terminal `CloseOp`
   only).
5. Build + broadcast `StateUpdateEvent` (with `closed=True` for
   terminal closes; carries `slot_counter`, `tracked_entities`,
   `tracked_services` for client convenience; *not* appended to
   `event_log`). Send `MutationCommittedResponse` to the mutator
   and `SessionEventFrame` to other subscribers; self-suppress the
   mutator's foreign frame.

All registry mutations across the file are sync and rely on GIL
atomicity for safety; there is no `WorkstationState.lock`. The
session worker serializes mutations per-session.

**Mutation ops** are a tagged union carried in
`MutateSessionRequest.ops`:

- `CloseOp` — terminal; drops the session, tears down all
  subscriptions, fires close-drain on trackers.
- `TrackEntityOp` / `UntrackEntityOp` / `TrackServiceOp` /
  `UntrackServiceOp` — manage the tracker channel via
  `_add/remove_session_{entity,service}_tracker` helpers (thin
  composites: pin-set update + incref/decref).
- `LoadEntityOp(slot, short_id)` — async load via `entities_scope.load_cbor` then `LoadResultEvent` with escaped CBOR.
- `LoadEntitySilentOp(slot, short_id)` — same machinery as
  `LoadEntityOp` but the success event is `LoadResultSilentEvent`
  with no payload. Phase 1 still walks the CBOR for FxtrID leaves
  and increfs the session, so child entities are tracked +
  refcounted identically; phase 3 short-circuits the wire-escape
  walk. Failures still deliver `LoadErrorEvent`.
- `InvokeOp(slot, service_short_id, method_name, args)` — async
  invoke; result lands as `InvokeResultEvent` / `InvokeErrorEvent`.
- `CallbackInvokeOp(slot, callback_slot, method_name, args)` — the
  `InvokeOp` variant targeting one of the session's *own callbacks*
  by slot (resolved against `pending_creates ∪ open_callbacks` in
  the post-pre-commit phase, like the arg-side `["callback", slot]`
  tag — so `[create_callback(N), callback_invoke(target=N, ...)]`
  rides one mutation). Both ops converge into the same invoke
  pipeline after target resolution. This is how clients call
  `subscribe_to` on their own inbox without a
  `GetCallbackServiceRequest` round trip.
- `CreateCallbackOp(slot)` / `CloseCallbackOp(slot)` —
  workstation-minted `WorkstationSessionEventInbox` callbacks (see
  `callback_impl.py`); slot lifetime spans `open_callbacks`. Closing
  also wakes the inbox's subscription parker tasks (unsubscribe on
  each `subscribe_to` source).
- `GetEntityMetadataOp(slot, short_id)` /
  `GetServiceMetadataOp(slot, short_id)` — sync resolve in the
  commit phase; result lands as `EntityMetadataResultEvent` /
  `ServiceMetadataResultEvent` (or error variants) on the session
  event log. Entity metadata payloads include `id_string`,
  `unprovenanced`, `provenance_parent` / `provenance_parent_id_string`,
  `unwraps_to` / `unwraps_to_id_string`, `context_data` (for `c`-tag
  data), and `backstep` (for `p`-tag data with a recorded backstep).
  Entity-ref-valued fields are emitted in list-escape-tagged form
  (`["entity", short_id]`) so the frontend's `unescapeWire` pipeline
  produces `EntityRefStub` instances directly — matching the wire
  shape of load / invoke / callback payloads. `*_id_string` fields
  stay as plain strings. Service metadata exposes `type_id_string`
  (plain string). Each `ProvenancedKey` surfaced in the payload is
  incref'd against the requesting session and tracked in
  `event_refs[seq]`.
- `PreserveEventsOp(seqs)` / `UnpreserveEventsOp(seqs)` — move
  events between `event_log` and the `preserved_events` sidecar.
  `event_refs[seq]` stays put while preserved so child refcount
  contributions persist. Validated against a working preserved set;
  duplicates within a single op's `seqs` list reject.

**Result-delivery workers.** `load_worker`, `invoke_worker`, and
`direct_load_worker` run on `state.background_tg`. The 3-phase
helper `_deliver_result_as_event(state, session_id, result,
source_scope, *, build_result_event)` is shared between invoke and
notify (callback) delivery, and maintains the **1-1 invariant**
between `TrackedEntityRecord` and entities reachable through the
record's scope (a record is never minted before its entity is in
scope, and never left behind without an entity):

- **Phase 1 (sync, atomic under session-alive gate)**: mint service
  records via `get_or_create_service_record` (spawns a worker on
  first creation) and incref the session. Collect entity FxtrIDs —
  entities aren't touched yet.
- **Phase 2 (async, parallel via task group)**: per service `await
  bridge_ready` (raise on `bridge_error`); per entity
  `_ensure_entity_record_via_copy` (or `_via_track` for same-store)
  in the same sync block as `_incref_session_entity` (session
  alive) or `_maybe_release_entity_record` (session is gone — the
  just-minted record has no holder so it's released, restoring the
  invariant).
- **Phase 3 (sync)**: encode CBOR via `wire_escape.escape` and
  append the result event via `_append_session_event`. Codec errors
  decref in-flight refs and re-raise.

Returns `True` on commit / `False` on aborted (session gone before
phase 1 or phase 3); raises on phase-2 bridge failure or phase-3
encode failure (after decref'ing in-flight contributions via
`_decref_in_flight_session_refs`). `deliver_invoke_result_to_slot`
is a thin wrapper that catches helper exceptions and emits an
`InvokeErrorEvent`; `WorkstationInboxImpl` lets the exception
propagate to the RPC caller. Open invoke / load slots are dropped
in each worker's `finally` block via `_drop_open_invoke_slot` /
`_drop_open_load_slot`.

**Resource-change buffer.** The `/client` snapshot+delta protocol
is driven by:

- `ResourceChangeBuffer` + `_TouchedRecord` — lazy pre-image capture
  per touched key.
- `WorkstationState.mutation_context()` — sync CM. Outermost entry
  allocates a buffer + flushes `ResourceChangesEvent` deltas on
  clean exit; nested entries are no-ops; exceptions drop the buffer
  un-flushed.
- `WorkstationState.touch(resource, key)` — must be called before a
  registry mutation; captures the pre-image.
- `WorkstationState._flush_buffer` — compares each touched key's
  pre-image against the post-mutation registry to derive `added` /
  `updated` / `removed`, then projects each (pre, post) pair
  through the subscription's `debug` flag (suppressing updates whose
  only diff is in fields the subscriber doesn't see) and emits one
  delta frame per affected resource per subscriber.

Every resource-mutating callsite calls `touch` immediately before
the mutation and runs inside some outer `mutation_context`. The
get-or-create helpers touch *before* minting so the buffer captures
"no record" as the pre-image. Result-delivery workers each open a
`mutation_context` around every sync window that touches state —
phase 1, phase 3, and error rollback. Phase 2 awaits sit between
contexts, so concurrent mutations from other paths can interleave.

**Snapshot builders + projection.** Per-record builders
(`_build_rpc_connection_record` / `_build_service_record` /
`_build_entity_record` / `_build_session_record`) plus the dispatch
`_build_record_for(state, resource, key)` power both the initial
`ResourceSnapshotEvent` and the buffer's pre/post lookup.
`_DEBUG_ONLY_FIELDS: dict[ResourceKind, frozenset[str]]` catalogs
the per-resource debug-only field names (`registering_connection_ids`
+ `system_source_kinds` + `pinned_by_sessions` for services;
`registering_connection_ids` + `pinned_by_sessions` for entities;
`service_registration_short_ids` + `entity_registration_short_ids`
for rpc_connections; empty for sessions). `_project_record(resource,
record, debug)` strips debug-only fields when `debug is False`.

**Subscription model.** `ConnectionSubscription` is a tagged union of
`ResourceSubscription | SessionSubscription | SessionDebugSubscription`.
State-level reverse indexes (`_resource_subs[ResourceKind]`,
`_session_subs[UUID]`, `_session_debug_subs[UUID]`) map the lookup
field to the set of `(connection_id, sub_id) → ClientConnectionRecord`
entries, so emit paths walk only the matching subscriptions. The
indexes are kept in sync via `WorkstationState._register_subscription`
/ `_unregister_subscription`.

**Session debug stream.** `SessionDebugSubscription` is a separate,
chatty, sequence-less, full-snapshot push of refcount + `slot_counter`
+ open-slot state. `_build_session_debug_snapshot(state, session,
sub_id)` constructs the snapshot (mapping `FxtrID` / `ServiceKey`
keys to workstation short_ids via the allocator) including
`preserved_events: list[int]` (sorted preserved seqs).
`_emit_session_debug_snapshot(state, session)` sends a fresh
snapshot to every active debug subscriber for that session. The
emit hook fires from refcount primitives, the `slot_counter` bump
in `apply_mutation`, `_close_session_drain_trackers`, and the
open-slot drop helpers.

### `short_ids.py` — `ShortIDAllocator`

Defines `ProvenancedKey` (frozen dataclass carrying `data_id` +
`prov_parent`, both bare FxtrIDs), `provenanced_key_for(fxtr_id)`
helper, and `ShortIDAllocator`. Two parallel allocators with
run-lifetime memory:

- **Services**: `s{N}` autoincrement. `forget_service` drops the
  mapping entirely and is wired to fire automatically via an
  end-callback on the underlying `IRHandle.token`, so the mapping
  is reaped once the handle's lifetime ends.
- **Entities**: per-`data_id` bucket with per-prov short IDs. No
  entity equivalent of `forget_*` — content-addressed entity IDs
  never go stale, mappings persist for the server's lifetime.

Each allocator separates "remembered" mappings from "active"
lookups: `deactivate_*` drops a key from active without forgetting
the mapping (so a later re-registration reuses the same short ID).

**Entity short-ID format:**

- **Prefixable** (`d`/`c`/`n` data tags). The bucket gets a
  hex-style prefix (`entity_short_id_prefix_len` chars by default;
  extended on collision against another data ID's bucket). The
  first `(data, *)` pair seen claims the bare prefix (e.g.
  `da1b2c`); subsequent distinct provenance parents claim `@2`,
  `@3`, …; a late-arriving `(data, None)` claims `@0`; the
  self-u-prov form `(data, u(data))` claims the special `@u` slot
  (the u-node's content is redundant with the data ID itself).
  `@1` is never allocated explicitly — the bare prefix is
  implicitly the `@1` form.
- **Non-prefixable** (`.`/`!`/`p`/`u` data tags). The bucket prefix
  is the full `data.id_string()`. `(data, None)` renders bare;
  `(data, u(data))` renders as `prefix@u`; `(data, prov)` for
  non-self-u prov renders as `f"{prefix}@{shortid_of((prov, None))}"`
  — the prov half's short is recursively allocated
  (`mark_active=False` so the prov key isn't promoted to "active").

**Lookup methods:**

- `lookup_active_entity(short_id) -> FxtrID | None` — returns the
  layered FxtrID (with `_provenance_parent` reconstructed) when
  active.
- `lookup_known_entity(short_id) -> ProvenancedKey | None` —
  resolves any short ID that's ever been allocated, for parsing
  composites whose prov half isn't itself active.
- `get_or_assign_entity_inactive(...)` — allocates the same
  run-lifetime short_id as `get_or_assign_entity` but does *not*
  mark the key active. Used by the out-of-band `LoadEntityRequest`
  path (`direct_load_worker`) for children short_ids that should
  appear as descriptors but aren't lookupable for further direct
  loads.

### `ws_app.py` — ASGI app

Starlette ASGI app built around a `WorkstationState` (attached as
`app.state.workstation_state`). Mounts `WebSocketRoute("/rpc",
rpc_endpoint)` and `WebSocketRoute("/client", client_endpoint)`. If
`debug_ui_dist_path` is provided and the directory exists, also
mounts `Mount("/debug", StaticFiles(..., html=True))`; if the path
is provided but missing, logs a warning and skips.

The `/rpc` endpoint enters `state.add_rpc_connection`, mounts a
fresh `WorkstationServiceImpl(state, record)` on `record.scope`,
wraps the WS through `_ws_transport.asgi_websocket_object_stream` +
`wrap_json_stream`, and opens `open_rpc_scope` with the impl's ref
as the main service.

### `client_protocol.py` — `/client` wire protocol

`msgspec.Struct` tagged unions defining the flat JSON wire protocol.
Client requests are tagged by `op`; server frames by `type`.

**Top-level requests** (handled directly, not via session mutation):

- `SubscribeRequest(resource, debug=False)` — `resource` is one of
  `"rpc_connections" | "services" | "entities" | "sessions"`.
  Server replies with `AckResponse` then pushes a
  `ResourceSnapshotEvent` (renamed from the historical
  `SnapshotEvent`), followed by per-mutation `ResourceChangesEvent`
  deltas carrying `added` / `updated` / `removed`.
- `UnsubscribeRequest` — shared across resource and session
  subscriptions.
- `CreateSessionRequest` → `SessionCreatedResponse`.
- `SubscribeSessionRequest` → server pushes `SessionSnapshotEvent`
  then `SessionEventFrame`s.
- `SubscribeSessionDebugRequest` → server pushes
  `SessionDebugSnapshotEvent` whenever debug-relevant state changes.
- `LoadEntityRequest(short_id)` → out-of-band direct entity load.
  Dispatched by `_handle_load_entity` in `client_app.py`: short_id
  resolved against the active entity allocator (miss → immediate
  `LoadEntityErrorResponse`); on hit, `direct_load_worker` is
  spawned on `state.background_tg` to do the async load + escape +
  reply. Opens a transient `load_scope` as a child of the parent
  record's scope, runs `load_cbor`, escapes via
  `get_or_assign_entity_inactive`, and writes a
  `LoadEntityResponse` (or `LoadEntityErrorResponse` on failure)
  directly to `record.send_nowait`. **No session linkage; no
  refcounts touched; no `TrackedEntityRecord` minted for children.**
  Children short_ids in the payload are descriptors for display.
- `GetCallbackServiceRequest(subscription_id, slot)` → sync lookup
  of the workstation short_id of a session-owned callback's
  send-side `WorkstationSessionEventInbox` service. Replies with
  `GetCallbackServiceResponse(service_short_id, ...)` on hit;
  unknown subscription / non-session subscription / unknown slot
  all surface via `ErrorResponse`. Dispatched by
  `_handle_get_callback_service` in `client_app.py`; reads
  `session.open_callbacks[slot].service_short_id` directly. Used
  by clients that need to expose a session-owned callback via /rpc
  without subscribing to the chatty `SessionDebugSnapshotEvent`
  stream — the agent MCP plugin uses it to discover the
  `workstation_inbox` callback's short_id at session bootstrap.
- `MutateSessionRequest(subscription_id, ack_seq, update_id, patch,
  ops)` → `MutationCommittedResponse` (mutator-only success) or
  `MutationRejectedResponse` (mutator-only rejection, carrying a
  typed `MutationRejectReason` — one of `stale_ack` / `conflict` /
  `validation_failed` / `callback_setup_failed` / `session_busy` /
  `session_gone` — plus a free-form `message`).

**`ErrorResponse`** is reserved for transport-level failures
(malformed JSON, unknown ops, missing fields) — it does *not*
surface mutation-pipeline rejections.

**`SessionEvent` union** (carried in `SessionEventFrame` for server
pushes) — `StateUpdateEvent | NonStateSessionEvent`, where
`NonStateSessionEvent` covers result + error + callback events:

- `LoadResultEvent` / `LoadResultSilentEvent` / `LoadErrorEvent`.
- `InvokeResultEvent` / `InvokeErrorEvent`.
- `EntityMetadataResultEvent` / `EntityMetadataErrorEvent` /
  `ServiceMetadataResultEvent` / `ServiceMetadataErrorEvent`.
- `CallbackEvent(seq, slot, payload)` — fires when an event is
  delivered into a workstation-minted callback inbox (direct-push
  `notify` or a `subscribe_to` subscription delivery).
- `CallbackSubscriptionEvent(seq, slot, connected, reason=None)` —
  connection-state transition for a callback inbox's `subscribe_to`
  subscription: `connected=True` when one is established,
  `connected=False` (with the keep-alive lease's break `reason`) when
  the source breaks ungracefully. Recoverable, so it's a normal seq'd
  event the client reduces into a per-inbox `idle`/`connected`/`broken`
  state (latest-wins) — unlike the terminal `ServiceBrokenNotification`
  side frame. An orderly inbox close emits no `connected=False`.

`SessionSnapshotEvent`'s non-state event lists
(`preserved_events`, `subsequent_events`) are typed
`NonStateSessionEvent` only; `StateUpdateEvent`s flow through the
snapshot's `last_state` field instead.

**Mutation ops** — see "Mutation ops" under `state.py` above.

`parse_text` / `serialize_to_text` wrap msgspec's JSON
encoder/decoder; `ProtocolError` translates msgspec's validation
errors so the read loop has a single error type to catch.

### `client_app.py` — `/client` endpoint

`client_endpoint` ASGI handler. Per-connection task structure:
enter `state.add_client_connection` → start a task group running
`_read_loop` (parses requests, dispatches to the per-op `_handle_*`
functions, enqueues ack/error responses) and `_write_loop` (drains
the per-connection memory stream into the WebSocket). Either loop's
exit cancels the task group's cancel scope, unwinding both.

Handlers:

- `_handle_subscribe` / `_handle_unsubscribe` — ack then push a
  `ResourceSnapshotEvent` (projected against the subscription's
  `debug` flag).
- `_handle_create_session` / `_handle_subscribe_session`.
- `_handle_mutate_session` — validates the mutator's session
  subscription locally, looks up the target session's
  `_PendingMutation` queue in `state.session_mutation_queues`, and
  `send_nowait`s the request. Backpressure (queue full) surfaces as
  `MutationRejectedResponse(reason="session_busy")`; missing /
  closed queue surfaces as `reason="session_gone"`; subscription
  mismatch as `reason="validation_failed"`. The session's own
  `session_worker` drains the queue and runs `apply_mutation`.
- `_handle_subscribe_session_debug` — mints a
  `SessionDebugSubscription` and pushes the initial snapshot via
  `_build_session_debug_snapshot`.
- `_handle_load_entity` — see `LoadEntityRequest` above.
- `_handle_get_callback_service` — see `GetCallbackServiceRequest`
  above.

**Scope discipline.** All workstation library code follows the
guidelines in [`v2/notes/scope_discipline.md`](../v2/notes/scope_discipline.md):
the only active-scope capture is at `WorkstationServer.__init__`'s
boundary; every internal scope is `isolated=True` with explicit
`include=`, and references are stored alongside their loadable
scopes (e.g. `RPCConnectionRecord.scope` for connection mounts).

### `protocols.py` — service protocols + namespace constants

- `WORKSTATION_NS` (stable namespace UUID),
  `WORKSTATION_SERVICE_TYPE_ID`, `RELEASE_CALLBACK_TYPE_ID`,
  `SESSION_EVENT_INBOX_TYPE_ID`, `SESSION_INBOX_SUBSCRIBER_TYPE_ID`,
  `PERSISTENCE_NO_PROV_TYPE_ID`, `PERSISTENCE_PRESERVE_PROV_TYPE_ID`.
- `ReleaseCallbackProto` — `release()` callable returned alongside
  every register/copy operation.
- `WorkstationSessionEventInbox[T]` — protocol for workstation-minted
  callback (inbox) services:
  `subscribe_to(event_source, method="subscribe", /, **kwargs) -> None`
  (durable feed: the workstation subscribes the inbox to a source and
  owns the subscription's lifecycle until the inbox closes;
  `event_source`/`method` are positional-only, `method` names the
  source's subscription method and is dispatched against the source as
  an *untyped* service, so the source need not be an `EventSource` —
  any `method(subscriber, **kwargs) -> UnsubscribeHandle` works,
  default `"subscribe"`; extra `**kwargs` are forwarded after the
  subscriber ref for parameterized subscription surfaces (whose extra
  params are keyword-only by convention — order-independent, no
  positional line-up with the subscriber-first slot), decoded by the
  source's own typed dispatch. Exactly-once: a second subscribe while
  one is live raises), `notify(payload) -> None` (direct push; silent no-op when
  closed), `listening() -> bool` (probe). Deliberately *not* a
  `Subscriber` (the push method is `notify`, not `on_event`), so an
  inbox ref can't be handed straight to `EventSource.subscribe` —
  durable subscriptions are forced through `subscribe_to`.
- `PersistenceProto` — protocol shared by both persistence-cap
  modes: `pin` / `unpin` / `retrieve` / `try_retrieve` / `list_pins`.
- `WorkstationServiceProto` — the workstation's main service
  surface. Currently exposes:
  - `name_connection(name)`.
  - `register_service(external_name, service)` → `(short_id,
    release_cb_ref)`; `get_service(short_id)`;
    `get_service_by_external_name(external_name)`.
  - `copy_entity(external_name, entity)` → `(short_id,
    release_cb_ref)`; `get_entity(short_id)`;
    `get_entity_by_external_name(external_name)`.
  - `lookup_short_id_by_external_name(external_name)` → `short_id`
    (looks up either kind — external names live in a **single
    namespace shared between services and entities**, so a name
    conflict between the two kinds is rejected at registration time
    just like a same-kind collision).
- `expose_service(...)` / `expose_entity(...)` —
  async-context-manager helpers, the **recommended client-side
  APIs**: explicit release on exit (`anyio.move_on_after`-guarded)
  plus an unconditional scope-side `forget_service` as a backstop.
  Sidestep the auto-release-on-drop cycle (see the caveat under
  `state.py` "TrackedServiceRecord").

### `service_impl.py` — server-side WorkstationServiceProto impl

Per-connection `WorkstationServiceImpl(state, record)` plus
`ReleaseCallbackImpl`. `register_service` allocates a short ID,
spawns a `service_record_worker` on `state.background_tg` if this is
the first registration for the `ServiceKey`, awaits `bridge_ready`,
appends a `Registration` to the record (and populates
`state.service_external_names[external_name]`, rejecting on
duplicate name), mounts a `ReleaseCallbackImpl` on the dispatch
scope, and registers an end-callback on the dispatch scope's token
so dropping the chain-bridged ref auto-releases (with the structural
cycle caveat called out under `state.py`).

`get_service` / `get_service_by_external_name` share
`_bridge_tracked_into_call_scope`, which awaits `bridge_ready`,
surfaces `bridge_error`, and bridges the workstation's stable
`service_scope` ref into the caller's dispatch scope.

On any failure between record-creation and registration-append, the
exception path rolls back the bare record via
`maybe_release_service_record` (the same conservative "if no
registrations and dict still points at us, pop" check, as a named
primitive) rather than leaking a stuck entry.

### `_ws_transport.py` — Starlette WS → fxtr stream adapter

`asgi_websocket_object_stream` wraps an already-accepted Starlette
WebSocket as `anyio.abc.ObjectStream[JSONTypes]`. Each text frame
carries one JSON message. Built on top of
`fxtr_sandbox_types.json_object_stream.JSONObjectStream`, so the
half-close protocol uses the same in-band `["!", "eof"]` sentinel
as the eryx transport. `WebSocketDisconnect` from the underlying
receive callable becomes `EndOfStream`; the WebSocket itself is
closed on context exit.

### `peer_rpc.py` — client-side `/rpc` connection helper

The public client counterpart of `_ws_transport.py`.
`open_workstation_rpc(rpc_url, *, name)` is an async CM that connects
to a workstation's `/rpc` endpoint over `httpx-ws`, layers the same
`JSONObjectStream` EOF discipline over the websocket, and opens an
fxtr2 RPC scope with no local main service, yielding
`RPCSession[WorkstationServiceProto]` (`remote_service` + `scope`) —
what demo orchestrators and tools use to register or look up external
services on a running workstation. Includes the httpx-ws
self-referential-exception-context workaround (same as
`fxtr_workstation_mcp.websocket_util` and `tests/_websocket_util.py`,
which keep their own copies so their dependency sets stay
independent). `httpx-ws` rides the `client` optional extra
(`fxtr_workstation[client]`), keeping the core runtime free of a ws
client; the module is import-on-demand and deliberately not
re-exported from `__init__`.

### `wire_escape.py` — list-based escape codec

Pure codec for the `/client` wire protocol. `escape(value, *,
encode_entity, encode_service)` walks a Python value, wraps every
literal list as `[[...]]`, and replaces `FxtrID` / `EntityRef` /
`ServiceRef` leaves with `["entity", short_id]` / `["service",
short_id]` tags. `bytes` leaves are encoded as `["bytes",
"<base64>"]` (mechanical, no callback). `unescape(value, *,
lookup_entity, lookup_service, lookup_callback=None)` is the
inverse; tagged values become bare `FxtrID`s / `ServiceRef`s /
`bytes` via the lookup callbacks (or base64-decode for `bytes`).

Reserved tag set is `{"entity", "service", "bytes", "callback"}`.
The `["callback", <slot:int>]` tag resolves through the optional
`lookup_callback: Callable[[int], ServiceRef[Any]] | None` (supplied
by `apply_mutation`'s invoke-arg validation phase); without one the
tag is rejected.

No I/O, no registry mutations — callers thread state through the
encode/lookup callbacks. The wire shape was inspired by Cap'n'Web's
escape rules but the rest of Cap'n'Web is *not* adopted; in code we
call this "list-based escape".

### `callback_impl.py` — workstation-minted inbox receive side

`WorkstationInboxImpl`
(`TypedServiceImpl[WorkstationSessionEventInbox]`), the receive-side
impl mounted by the workstation when a session creates a callback.
Carries `(state, session_id, slot, impl_scope, _closed,
_subscriptions)`. `listening()` returns `not _closed`.
`notify(payload)` silently no-ops when `_closed` and otherwise
captures the dispatch `call_scope` via `get_active_scope()` and runs
the shared 3-phase delivery pipeline `_deliver_result_as_event` with
a `CallbackEvent`-building lambda (returns `None`; session-gone is
also a silent drop). Exceptions from the delivery pipeline propagate
to the RPC peer (no session-side error event).

`subscribe_to(event_source)` is the durable-feed path, **exactly
once**: an inbox holds at most one *live* subscription (one that is
neither `done` nor `broken`), so any `subscribe_to` while one is live
raises — including a repeat for the same source. On a fresh subscribe
it spawns a per-subscription **parker task** on `state.background_tg`
via `await background_tg.start(...)` (mirroring
`callback_record_worker`). The parker owns the subscription
end-to-end: it opens an isolated mountable subscription scope
(`include=[impl_scope]`), bridges the source in from the dispatch call
scope, **registers a broken-callback on the bridge's keep-alive lease**
(`_on_inbox_subscription_broken`, mirroring `service_record_worker`),
mounts an internal `_InboxSubscriberImpl` (whose `on_event` runs the
same 3-phase delivery; silent drop when closed), calls
`source.subscribe(subscriber_ref)` (re-typing the ref to the fxtr2
`EventSource` protocol), signals `started()` (failures before this
point propagate to the invoking client as an `invoke_error`), and
parks on the entry's `unsubscribe_requested` event. On a successful
establish, `subscribe_to` appends a `CallbackSubscriptionEvent(connected=True)`
(via `state._emit_callback_subscription_event`) ahead of its own
invoke result. On wake (or task-group cancel) the parker attempts a
best-effort `handle.unsubscribe()` (shielded, bounded by
`UNSUBSCRIBE_TIMEOUT_SECONDS`) — **skipped when the entry is `broken`**
(the source is already dead) — then tears the subscription scope down
**forcefully** — the durable "you're done" signal for sources that
missed the unsubscribe, and the breaker for the cross-pin cycle
between the two sides' per-subscription scopes.

`_on_inbox_subscription_broken(inbox, entry, reason)` is the
lease-broken callback: sync + idempotent (no-op if `entry.broken` /
`done`), it marks the entry `broken`, emits a
`CallbackSubscriptionEvent(connected=False, reason=...)` to the owning
session, and fires `unsubscribe_requested` to wake the parker for
teardown. An *orderly* close emits no break event.

`mark_closed()` (called by the `CloseCallbackOp` commit and the
session-close sweep) flips `_closed` and fires every subscription's
`unsubscribe_requested` event — sync and idempotent; the parkers do
the async work.

The receive-side lifecycle (`CallbackRecord`, `open_callbacks`, the
worker, and the `CreateCallbackOp` / `CloseCallbackOp` mutation
ops) lives in `state.py` / `client_protocol.py`. The worker
(`callback_record_worker`) opens an isolated mountable
`impl_scope`, mounts a `WorkstationInboxImpl`, runs
`get_or_create_service_record`, awaits the bridge, and parks on
`shutdown_event`. The `CallbackRecord` is fully formed at
construction — `impl` / `tracked` / `service_key` /
`service_short_id` are all required fields, populated before the
worker signals readiness via `task_status.started(callback_record)`.
There is no dedicated "callback ready" wire event — by the time
the mutation's `StateUpdateEvent` commits, the callback is already
usable.

### `persistence.py` — persistence cap machinery

Defines:

- `PersistenceNamespaceConfig(external_name, namespace, mode)` —
  frozen dataclass passed to `WorkstationServer`. The user opens
  `FxtrNamespace` instances externally; the workstation just
  attaches to their scope.
- `NoProvPersistenceImpl` — `TypedServiceImpl[PersistenceProto]`
  forwarding `pin` / `unpin` / `retrieve` / `try_retrieve` /
  `list_pins` straight through to the underlying namespace,
  threading `call_scope = get_active_scope()` for both pin
  (`scope=call_scope`) and retrieve (`into_scope=call_scope`,
  `copy_into_scope=False` ⇒ lift via inheritance).
- `PreserveProvPersistenceImpl` — same protocol, distinct
  `service_type_id`. Pin path: `_layer_for_storage` extracts the
  prov half of layered inputs via
  `call_scope.extract_provenance_parent` or lifts bare data to a
  u-tag prov node via `call_scope.create_u_node` (rejecting bare
  prov-tag inputs as ambiguous); pins the bare prov node into the
  namespace. Retrieve path: `_restore_layered` validates that the
  namespace returned a prov-tag node and re-layers via
  `call_scope.embed_as_provenance_parent`. `unpin` / `list_pins`
  are mode-agnostic.
- `persistence_cap_worker(state, config, *, task_status)` —
  background task spawned by `WorkstationServer` per config. Opens
  an isolated mountable `impl_scope` (`include=[root_scope]`),
  mounts the impl, runs `get_or_create_service_record` to bridge
  the impl ref into the cap's `service_scope`, awaits
  `bridge_ready`, installs a `Registration(source=SystemSource("persistence",
  metadata=...), external_name=...)`, signals
  `task_status.started()`, and parks on `tracked.shutdown_event`.
  Mirrors `callback_record_worker`'s shape.

### `external_services.py` — user-supplied external services

Defines:

- `ExternalServiceConfig(external_name, service, scope)` — frozen
  dataclass passed to `WorkstationServer`. The user opens `scope`
  and mounts `service` (or otherwise makes it reachable)
  externally; the workstation just bridges the ref in.
- `attach_external_service(state, config) -> None` — async inline
  helper called by `WorkstationServer` once per config during
  startup (no per-config worker; there's no per-config scope to
  own). Bridges `config.service` from `config.scope` into
  `state.root_scope` via `bridge_service_from_scope` (this pins the
  user's scope until `root_scope` drains), mints the
  `TrackedServiceRecord` via `get_or_create_service_record`,
  awaits `bridge_ready`, and installs a
  `Registration(source=SystemSource(kind="external",
  metadata={"external_name": ...}), external_name=...)` with
  rollback on bridge or registration failure. Shutdown uses the
  standard `SystemSource` release sweep — no module-specific
  teardown.

### `__init__.py`

Re-exports `WorkstationServer`, `PersistenceNamespaceConfig`,
`PersistenceProto`, `ExternalServiceConfig`, `expose_service`, and
`expose_entity`.

### `dynamic_dispatch/` — runtime-loaded frontend renderers

Backend half of the dynamic dispatch system: a typed service
protocol that the frontend's `createDynamicDispatcher` invokes to
look up a renderer for a given entity / service type, plus two
concrete implementations that build renderer modules and broadcast
reload notifications to subscribed frontends. The frontend half lives in
`frontend_lib/packages/fxtr-ui-react-dispatch/src/dynamic_dispatcher.ts`
and consumes the same wire shapes.

Two impls share the unchanged `WorkstationDispatcherProto` surface:
`basic_dynamic_dispatch/` (walks on-disk renderer dirs + runtime `npx
esbuild` — **no longer the suggested impl**, kept for unmigrated
demos/harnesses) and `nix_configurable_dispatch/` (loads a preset by **git
reference**, builds renderers via `lib.fxtr.esbuildRenderer`, mints renderer
entities at serve time — the nix/git-driven path).

Submodules:

- `dynamic_dispatch_types.py` — the public protocol + entity
  shapes. `WorkstationDispatcherProto` is a `@service_protocol`
  with two methods:
  - `resolve(type_ref: EntityRef[UnknownEntity], dispatch_key_uuid: str) -> RendererResolution | None`.
    A `None` result is the backend's "no renderer for this
    (type, dispatch key) pair" signal; the frontend caches the
    null and surfaces it via the dispatch key's `NoMatchFallback`.
    The per-key argument lets backends route different dispatch
    keys for the same type to different renderer modules — handy
    when shards of one entity's visualizers live across several
    files. Backends that don't care can simply ignore the arg.
    `RendererResolution` is a frozen dataclass carrying
    `{subtype_key: str, renderer: EntityRef[RendererEntity]}` —
    the `subtype_key` is opaque to the frontend (the backend
    picks whichever label it wants); the `renderer` is an
    `EntityRef` whose loaded shape is `{module: bytes}`.
  - `subscribe(subscriber: ServiceRef[Subscriber[EntityRef[ReloadEverythingNotification]]]) -> ServiceRef[UnsubscribeHandle]`.
    Inherited from the proto's
    `EventSource[EntityRef[ReloadEverythingNotification]]` base
    (genuine subclassing since fxtr2 stage 13 part 0; re-declared on
    the proto purely for its docstring — wire dispatch is by method
    name, so refs re-typed to `EventSource` still reach it).
    The dispatcher delivers a `ReloadEverythingNotification` entity
    ref to the subscriber on every renderer-table change until
    unsubscribed. The frontend allocates one inbox per dispatcher
    session, routes the subscription through the inbox's
    `subscribe_to` (the workstation owns the lifecycle), and treats
    every received event as "drop the per-type caches and
    re-resolve every entry."

  `NixDispatcherProto` is a second `@service_protocol` — a genuine
  subclass of `WorkstationDispatcherProto` (which is marked
  `allow_protocol_subtypes=True`): `resolve` / `subscribe` are
  inherited, and it adds
  `set_preset(git_commit, flake_attr, flake_path=None)`.

  `RendererEntity` is the matching `DataclassEntity`.
  `ReloadEverythingNotification` is an empty `DataclassEntity`
  used as the broadcast payload — two instances content-hash-dedup
  to the same FxtrID, so broadcasting one per notification doesn't
  accumulate distinct entity records on the workstation. Module-
  level constants `DYNAMIC_DISPATCH_NS`,
  `WORKSTATION_DISPATCHER_TYPE_ID`, `RENDERER_ENTITY_TYPE_ID`, and
  `RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID` give the namespace +
  type identities. **These entity types + type-id constants are
  defined in `fxtr2.experimental.dynamic_dispatch`** (so a serving
  layer can mint/serve them without depending on the workstation) and
  re-exported here; `dynamic_dispatch_types` itself now holds only the
  protocols + `RendererResolution`.
- `basic_dynamic_dispatch/` — one concrete dispatcher impl.
  - `compile.py` — async esbuild wrapper.
    `await compile_renderer(directory)` uses
    :func:`anyio.run_process` to shell out to
    `npx --yes esbuild <directory>/index.tsx --bundle
    --format=esm --platform=browser --jsx=automatic
    --external:<each>` and returns the ESM bytes. Async so the
    surrounding event loop stays responsive while esbuild runs
    (~few-hundred-ms even for tiny renderers).
    `npx_available()` + the `RendererCompileError` exception
    class give test code + callers a clean failure handle for
    environments without Node.js. The default external set
    (`react`, `react/jsx-runtime`, `jotai`, `jotai-effect`, the
    four `@fxtr-ui/*` packages plus `@fxtr-ui/workstation-dispatch`)
    matches the frontend's import-map shim — anything in this
    list stays as a bare-specifier import in the compiled module
    and resolves at runtime through the host's bundled instance.
  - `service_impl.py` — `BasicDynamicDispatchImpl` +
    `mount_basic_dynamic_dispatch`.

    **Constructor** takes
    `(scope, renderer_dirs, impl_tg, externals?, subtype_key?)`.
    `impl_tg` is a caller-owned :class:`anyio.abc.TaskGroup` the
    impl's `SubscriberHub` spawns per-subscription tasks on; on
    shutdown the mount harness calls `impl.aclose_subscriptions()`
    (the hub's flushing `aclose`) bounded by
    `SUBSCRIPTION_FLUSH_TIMEOUT_SECONDS`, then cancels `impl_tg`
    (the forceful backstop — a no-op when the flush completed). The
    caller awaits
    `impl.compile_and_register()` to walk every `renderer_dirs[i]`,
    treat each subdirectory's name as a type-id-string bucket,
    compile `<subdir>/index.tsx`, mint a `RendererEntity` in
    `scope`, and build the
    `type_id_string -> RendererResolution` lookup table. A
    per-subdir mtime snapshot (`self._mtimes`) is captured at
    register-time and consulted by
    :meth:`recompile_changed_and_reload`.

    **Hot-reload surface**:
    - `subscribe(subscriber)` — typed-service method delegating to
      a coalesce-mode
      `fxtr2.services.subscription_util.SubscriberHub` held by the
      impl (`task_group=impl_tg`; the hub owns no scope). The hub
      owns the per-subscription worker/holder task + scope + bridge
      machinery and the mounted `UnsubscribeHandle`; the
      subscription is fully established before the method returns,
      and does not pin `self._scope`.
    - `trigger_reload()` — sync method. Mints a fresh
      (content-hash-deduped) `ReloadEverythingNotification` in
      `self._scope` and `hub.publish`es its ref with
      `origin_scope=self._scope` (each pending event carries an
      origin lease so the notification stays loadable until
      delivered or superseded). Multiple triggers during one
      in-flight delivery collapse to one send per subscriber
      (coalesce-mode skip-ahead).
    - `recompile_changed_and_reload()` — async method. Re-scans
      `renderer_dirs`, compares each subdir's `index.tsx` mtime
      to `self._mtimes`, recompiles changed/added subdirs +
      drops vanished ones from `self._table`, then calls
      `trigger_reload()` iff anything changed. Returns `True` on
      change. Idiomatic driver for a polling task in iteration-
      loop tooling (the dynamic-dispatch-demo backend wires this
      up).

    `resolve()` reads `type_ref.entity_id.id_string()` from the
    wire call and looks the resolution up; misses return `None`.
    `IRException(category="DispatcherNotRegistered")` is reserved
    for the programming-error case where `resolve()` /
    `recompile_changed_and_reload()` is invoked before
    `compile_and_register()` completes. The default subtype key
    is `"default"`; the frontend treats it opaquely so backends
    are free to introduce richer labelling later.

    **Mount helper**: `mount_basic_dynamic_dispatch(renderer_dirs, *, parent_scope=None, externals=DEFAULT_EXTERNALS, subtype_key=DEFAULT_SUBTYPE_KEY, scope_name=...)`
    is the ergonomic async CM that owns the scope + task group +
    runs `compile_and_register` + mounts the impl. Yields a
    `MountedBasicDynamicDispatch` dataclass carrying
    `(scope, service_ref, impl)` — the caller plugs `service_ref`
    + `scope` into an `ExternalServiceConfig` and keeps a handle
    on `impl` for driving `trigger_reload()` /
    `recompile_changed_and_reload()` from outside. On exit
    cancels the task group + drains the scope.

Mount pattern (preferred): open the helper CM, hand the yielded
`mounted.service_ref` + `mounted.scope` to
`WorkstationServer(external_services=[ExternalServiceConfig(...)])`.
The renderer entities the impl mints live in
`mounted.scope`; the workstation's external-service bridge pins
that scope for the workstation's lifetime so the entities stay
reachable for any `get_entity_by_external_name`-style resolves
and for the frontend's `loadEntityData` calls when it fetches
renderer bytes. Callers who want explicit control of the scope
and task group can construct `BasicDynamicDispatchImpl` directly
and pass their own `impl_tg`; see the impl's docstring for the
usage skeleton.

- `nix_configurable_dispatch/` — the nix/git-driven dispatcher impl.
  - The preset eval/build nix layer (`eval_preset_table(h, commit, attr,
    flake_path?)` + `build_drv_outputs(h, drv_paths)`, both taking a
    `fxtr_nix_runner` backend **handle** `h`) lives in
    **`fxtr_nix_runner.renderers`** — it's flake-agnostic preset-building, shared
    with that package's own renderer read adapter. `__init__` imports both from
    there. See the `fxtr_nix_runner` CODE_OVERVIEW for their shapes.
  - `service_impl.py` — `NixConfigurableDispatchImpl` +
    `mount_nix_configurable_dispatch`. **Constructor**
    `(scope, repo, impl_tg, *, initial_commit=None, initial_attr=None,
    initial_flake_path=None)`; `repo` (the git repo preset refs resolve against
    — locally the agent's workdir) is required. The backend resolves **lazily**
    on first `set_preset` (a cached `_backend()` accessor), so constructing a
    dispatcher needs no backend config; `set_preset` uses a per-call
    `builder_for` (a persistent env-lifetime handle is a deferred optimization —
    see `stage9post1_backend_config_wizard.md`). Emits
    `WORKSTATION_DISPATCHER_TYPE_ID` (frontend's `replace_target_type`
    unchanged); the agent re-types to `NixDispatcherProto` for `set_preset`.
    - `set_preset(commit, attr, flake_path)` — under one
      `self._backend().builder_for(repo)` handle (host nix or in-podman),
      eval the table, then realize **all** needed drvs (every `typeEntity`
      layer + every uncached renderer) in **one** `build_drv_outputs` call
      (nix-parallel, each output materialized). Then, from the materialized
      outputs (read after the builder is released): **eagerly** validate each
      key (layer entities: read the `typeEntity` layer via
      `LayeredFileStoreReader` over its materialized `host_path` + remap,
      assert its pinned `id_string` == the type-key; **builtin** entities:
      skip the build and assert the carried `idString` == the type-key —
      `_validate_builtin`;
      mismatch either way → `IRException(category="RendererMisconfigured")`,
      whole-preset reject) and mint/cache a `RendererEntity` per
      `(commit, rendererDrv)` in `scope`. Builds/mints stay in locals; `_table` / `_current_ref` swap
      atomically only after every entry succeeds, then `trigger_reload()`.
      Validation is **eager** (not request-time) because lazy resolve-time
      validation can't catch a stale key — the mis-keyed entry never
      resolves (see `06_ui_presets.md`).
    - `resolve(type_ref, uuid)` — a **pure lookup**
      (`_table[id_string][uuid]`); no build / validation. An empty table
      (no preset pushed) resolves everything to `None`.
    - `initialize()` — applies the optional initial `(commit, attr)` so
      restarts come up with a UI. **Always warn, never crash:** any
      auto-load failure logs a warning + comes up empty; `current_ref`
      records the attempted ref for the empty-table debug surface. (An
      agent-invoked `set_preset` always raises.)
    - `subscribe` / `trigger_reload` — same coalesce-mode
      `SubscriberHub` delegation as `BasicDynamicDispatchImpl`.
  - **Mount helper** `mount_nix_configurable_dispatch(repo, *,
    parent_scope=None, initial_commit=…, initial_attr=…, initial_flake_path=…,
    scope_name=…)` — owns the scope + task group, runs `initialize()`, mounts
    the impl `replace_service_type(WorkstationDispatcherProto)`, yields a
    `MountedNixConfigurableDispatch(scope, service_ref, impl)`. Same
    `ExternalServiceConfig` plug-in pattern as the basic helper.

### `debug_ui/src/` — Vite + React + TS debug viewer

Small standalone app served at `/debug`. Subscribes to all four
resources with `debug=true`. `ws_client.ts` matches the
snapshot+delta wire (subscriptions return a single `onSnapshot`
callback that re-fires after each `resource_changes` delta is
merged against a local copy keyed off each resource's primary-key
field) and exposes:

- `subscribeSession` — folds incoming `session_event` frames into a
  maintained `{last_state, events}` snapshot. Non-state events
  append to `events`; `state_update` events update `last_state`
  authoritatively without appending. `slot_counter` rides
  `last_state`. The initial snapshot flattens the wire's
  `preserved_events` + `subsequent_events` buckets into the
  renderer's single ordered `events` list.
- `subscribeSessionDebug` — pass-through of the latest
  `session_debug_snapshot` frame.

`App.tsx` renders the rpc-connections / services / entities tables
followed by a `SessionsPanel` (filterable list with checkboxes;
each checked row inflates a `SessionSubPanel`). The sub-panel has
four sections:

- **Last state** — collapsible JSON tree of `last_state.state` via
  `JsonTree`.
- **Session pins** — two small tables (entities and services)
  filtered from the global tables by `pinned_by_sessions`
  membership, with refcount badges sourced from the parallel
  `subscribe_session_debug` stream and an `explicit pin` /
  `retained-event only` source label derived from
  `last_state.tracked_*`.
- **Open slots** — three small tables (callbacks / invokes / loads)
  populated from the debug snapshot's `open_callbacks` /
  `open_invokes` / `open_loads` fields.
- **Event log** — timeline of the unacked-suffix plus `last_state`,
  with state_updates rendered twice (a collapsible pending row at
  sort-key `ack_seq` and a one-line committed marker at sort-key
  `seq`); result/error events as single rows. Three-way row
  coloring: preserved seqs render in orange with a `preserved`
  chip, rows past the horizon but not preserved render gray, and
  unacked-suffix rows render normally. `SingleEventRow`
  short-circuits the expand/collapse affordance for
  `load_result_silent` (no body to expand).

`ServicesTable` renders `system:{kind}` chips per kind in the
`registered by` column for `system_source_kinds`, before the
existing connection-ID badges. Persistence caps and external
services surface here.

## Key Concepts

These are sketched in [`plans/phase0/part1_ws_server/overview.md`](plans/phase0/part1_ws_server/overview.md);
the short version:

- **Tracked entities / services.** Two run-lifetime registries on
  `WorkstationState`. Service short IDs are `s{N}` autoincrement.
  Entity short IDs encode a `ProvenancedKey = (data_id, prov_parent)`
  pair: prefixable (`d`/`c`/`n`) data IDs use a hex prefix with
  `@N` enumeration for distinct provenance variants (`@1` reserved
  as the implicit bare form, `@0` for late-arriving
  `(data, None)`); non-prefixable IDs use the full id-string with a
  recursively-allocated `@<prov-short>` suffix for layered keys.
  Service mappings are forgotten when the underlying handle's
  lifetime token ends; entity mappings persist for the server's
  lifetime (content-addressed IDs never go stale).
  External-name namespaces are exclusive (and separate between
  services and entities).
- **Records vs registrations.** A tracked record has many
  registrations, each owning a release callback. Sources include
  RPC connections and workstation-internal mounts (`SystemSource`).
  Sessions track records via a separate channel; a record stays
  alive while either channel is non-empty.
- **Per-record entity scopes.** Each `TrackedEntityRecord` owns its
  own isolated, non-mountable `FxtrScope` (child of `root_scope`)
  holding the layered FxtrID. There is no global `entities_scope`
  umbrella. Releasing a record drains its scope via `force_close`;
  the v2 arena's per-scope refcounting keeps overlapping data
  subgraphs alive across coexisting `(data, prov_X)` /
  `(data, prov_Y)` records. RPC `get_entity` uses
  `call_scope.track_from_scope` from the record's scope (sync,
  same-store, no copy). Invoke / callback dispatches open
  `call_scope` with `include=[target_scope, *arg_service_scopes,
  *arg_entity_scopes]` so every entity-typed arg is reachable
  through inheritance.
- **Two WebSocket surfaces.** `/rpc` runs `open_rpc_scope` for
  fxtr-RPC peers; `/client` runs the custom session-aware protocol
  (resource subscriptions + sessions + out-of-band entity load).
- **Sessions = unit of stateful interaction.** A session carries
  user-driven JSON state (mutated via JSON Patch ops) plus a
  monotonic event log of side-effect results (loads, invokes,
  metadata, callback notifications). Sessions explicitly track
  entities / services to keep them alive; retained event payloads
  also pin children via per-event refcount sidecars; `preserve_events`
  holds events past the ack horizon. Closing a session drains all
  refcounts and tears down callbacks. See
  [`plans/phase0/part1_ws_server/stage3_sessions.md`](plans/phase0/part1_ws_server/stage3_sessions.md)
  for the full invariants.
- **In-memory by default.** Workstation state evaporates on
  restart. Durability is opt-in via `PersistenceNamespaceConfig`
  (the user supplies a `FxtrNamespace`, the workstation mounts a
  `PersistenceProto` cap as a workstation-internal service). The
  separate `ExternalServiceConfig` route lets the user mount any
  pre-built service as a workstation-internal registration.
