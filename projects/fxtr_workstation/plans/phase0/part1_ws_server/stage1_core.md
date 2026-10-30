# Stage 1 — Core fxtr-RPC layer + tracking

> **Status: working document.** This stage is large enough that we're
> pair-programming it rather than writing a frozen spec. This plan
> captures the design as currently understood; we update it as we go.
> Anything in *italics* is a known unknown waiting to be resolved.

Goal: stand up a workstation server that listens on `/rpc`, hosts a `WorkstationServiceProto`, and lets RPC peers register services and entities, look them up by short ID or external name, and observe each others' registrations across separate RPC connections. No `/client` endpoint, no sessions, no debug UI in this stage.

The deliverable is "two RPC clients connect, one registers a service and an entity, the other retrieves them by name and by short ID, and the lifetimes drain cleanly when each side detaches."

## Public surface

```python
async with WorkstationServer(host="127.0.0.1", port=0) as ws:
    print(ws.bound_url)         # e.g. ws://127.0.0.1:34521
    print(ws.rpc_url)           # ws://.../rpc
    # ... tests do their thing ...
# server is fully drained on context exit
```

`WorkstationServer.__init__` accepts:
- `host: str = "127.0.0.1"`
- `port: int = 0` (`0` → ephemeral; `bound_url` reflects what hypercorn actually picked)
- `entity_short_id_prefix_len: int = 7`
- `name: str = "workstation"` (used in scope / lifetime names for logging)

The server enters its mountable root scope on `__aenter__` and starts hypercorn on its task group; `__aexit__` triggers the hypercorn `shutdown_trigger` event, awaits hypercorn's drain, then closes scopes innermost-first.

## Module layout (initial)

All new code lives under `projects/fxtr_workstation/fxtr_workstation/`. Layout will likely shift as we go:

```
fxtr_workstation/
├── __init__.py              # re-exports WorkstationServer + the public protocol types
├── server.py                # WorkstationServer ctx mgr, hypercorn glue
├── state.py                 # WorkstationState + record dataclasses; the in-memory registries
├── short_ids.py             # ShortIDAllocator (entities + services)
├── protocols.py             # WorkstationServiceProto + ReleaseCallbackProto
├── service_impl.py          # WorkstationServiceImpl (the per-RPC-connection main service impl)
├── ws_app.py                # Starlette app; /rpc route that drives open_rpc_scope per connection
└── _ws_transport.py         # ASGI websocket -> ObjectStream[JSONTypes] adapter
```

Tests live in `projects/fxtr_workstation/tests/`. The existing `test_smoke.py` stays.

## Data model

`state.py` holds the in-memory state. All mutations happen behind a single `anyio.Lock` to keep the fast-path simple — the lock is only ever held for non-IO bookkeeping; long-running work happens outside.

The tracking model separates "this resource is tracked by the workstation" (the **record**) from "someone has bound this resource by name and holds a release callback" (the **registration**). A record can have multiple registrations from multiple sources (e.g. RPC bounceback: A registers; B retrieves and re-registers; eventually some workstation-internal registrant once stage 6 lands). Each source is a `RegistrationSource` — for now always an `RPCConnectionRecord`, later a union with workstation-internal sources.

```python
@dataclass
class Registration:
    """A single external binding to a tracked record. Holds a release-callback."""
    external_name: str | None      # None means "id-only" registration (rare in stage 1)
    source: RegistrationSource     # currently: RPCConnectionRecord
    # release-callback bookkeeping (token end-callback, etc.)

@dataclass
class TrackedEntityRecord:
    fxtr_id: FxtrID
    short_id: str
    registrations: list[Registration]    # empty when only sessions / slots are pinning it
    # any other tracker source kept as separate refcounts (sessions, slots) — added in stage 3+
    # the entity has been copied into WorkstationState.entities_scope; abandon() when fully untracked

@dataclass
class TrackedServiceRecord:
    service_key: ServiceKey
    short_id: str                  # "s0", "s1", ...
    registrations: list[Registration]
    # The service is mounted/bridged inside service_scope. service_scope is owned by a
    # per-record anyio worker task that opens it and waits on a "shutdown" event; when
    # the record's last tracker drops, we set the event and the task tears the scope
    # down. See "Per-service-record lifetime" below.
    shutdown_event: anyio.Event
    # bridge_ref: ServiceRef[Any] — produced inside the worker task once the bridge is established

@dataclass
class RPCConnectionRecord:
    connection_id: UUID
    peer_addr: str | None
    connected_at: datetime
    name: str | None                   # set via name_connection; mutable, last-writer-wins
    # The connection record owns the user_scope from open_rpc_scope; per-connection
    # WorkstationServiceImpl is mounted on a child scope of that user_scope (NOT on
    # WorkstationState.root_scope) so the impl's lifetime tracks the connection.
    registrations: list[Registration]  # what this connection sources; freed on disconnect

@dataclass
class WorkstationState:
    entities_scope: FxtrScope          # the workstation-wide tracked-entities scope
    root_scope: FxtrScope              # parent of everything; owns the task group hypercorn runs on
    entity_records: dict[FxtrID, TrackedEntityRecord]
    service_records: dict[ServiceKey, TrackedServiceRecord]
    rpc_connections: dict[UUID, RPCConnectionRecord]
    short_ids: ShortIDAllocator
    lock: anyio.Lock
```

### Per-service-record lifetime

Each `TrackedServiceRecord` runs a small worker task on `state.root_scope`'s task group. The task is roughly:

```python
async def service_record_task(record: TrackedServiceRecord, source_ref: ServiceRef[Any]) -> None:
    async with FxtrScope(isolated=True, mountable=True, name=...) as service_scope:
        # bridge source_ref into service_scope
        record.bridge_ref = await service_scope.copy_from_scope(source_ref)
        record.bridge_ready.set()
        await record.shutdown_event.wait()
    # service_scope drains here on CM exit
```

`register_service` waits on `record.bridge_ready` before returning. Any later code that wants to drop the record sets `record.shutdown_event`. The drain happens naturally as part of the CM exit; we don't need a separate "draining services" map.

This sidesteps the ad-hoc "kick off `aclose()` and watch it" pattern in earlier drafts.

## Short-ID allocation (`short_ids.py`)

Two parallel allocators, both with run-lifetime memory:

- **Entities**: keyed by `FxtrID`. For `d`/`n`/`c` nodes the candidate is the first `prefix_len` chars of `id.idString()`; on collision with another *active or remembered* short ID, extend by one char and try again. For other node types use the full ID string. Once allocated, the mapping is remembered for the lifetime of the server even after the entity becomes untracked. Re-tracking returns the same short ID.
- **Services**: keyed by `ServiceKey`. Allocate `s{N}`, autoincrement; remember while the underlying `IRHandle`'s token is alive. We subscribe to the token's end-callback to drop the map entry; that ensures we don't leak the mapping forever.

Pseudocode:

```python
class ShortIDAllocator:
    def get_or_assign_entity(self, fxtr_id: FxtrID) -> str: ...
    def get_or_assign_service(self, service_key: ServiceKey) -> str: ...
    def lookup_entity(self, short_id: str) -> FxtrID | None: ...
    def lookup_service(self, short_id: str) -> ServiceKey | None: ...
    def forget_service(self, service_key: ServiceKey) -> None: ...   # called from token end-cb
```

The allocator owns two dicts in each direction so lookups are O(1) once allocated. Active-vs-remembered distinction is captured by an "active" set; `lookup_*` consults active only.

## Service protocol (`protocols.py`)

```python
@service_protocol
class ReleaseCallbackProto(Protocol):
    """Returned by register_service / copy_entity. Releases the registration when invoked
    *or* when the bridge is dropped (whichever happens first)."""
    async def release(self) -> None: ...

@service_protocol
class WorkstationServiceProto(Protocol):
    async def register_service(
        self, external_name: str, service: ServiceRef[Any]
    ) -> tuple[str, ServiceRef[ReleaseCallbackProto]]: ...
    async def copy_entity(
        self, external_name: str, entity: FxtrID
    ) -> tuple[str, ServiceRef[ReleaseCallbackProto]]: ...
    async def get_service(
        self, service_short_id: str
    ) -> ServiceRef[OverrideThisTypeBeforeUsing]: ...
    async def get_service_by_external_name(
        self, external_name: str
    ) -> ServiceRef[OverrideThisTypeBeforeUsing]: ...
    async def get_entity(self, entity_short_id: str) -> FxtrID: ...
    async def get_entity_by_external_name(self, external_name: str) -> FxtrID: ...
    async def name_connection(self, name: str) -> None: ...
```

Notes:
- `external_name` is per-registration. `get_*_by_external_name` resolves to the *most-recent* registration that bound that name and is still active. *(open: do we want collision semantics on duplicate external_names? for stage 1 the simplest thing is "last writer wins on lookup, no error on register"; revisit if real use cases push back)*
- The `service` arg is a generic `ServiceRef[Any]`; the typed-service framework decodes the wire arg into one. For `entity` we accept both `FxtrID` and `EntityRef` at the protocol-decoder level — the dialect strips the `EntityRef` annotation transparently.

## `WorkstationServiceImpl` (`service_impl.py`)

One impl per RPC connection. Mounted on a *per-connection* scope inside `rpc_endpoint`'s body, not on `state.root_scope`. Holds back-references to `WorkstationState` and to its `RPCConnectionRecord`. *The exact scope nesting here is one of the trickier parts and we'll iterate on it in implementation.*

Per-method sketch:

- **`register_service(name, ref)`**:
  1. Resolve the `ServiceKey` for `ref`. Allocate / look up the service short ID.
  2. If a record already exists for this `ServiceKey`, append a new `Registration` to its `registrations` list; otherwise spawn the per-record worker task (above) to bring up `service_scope` and bridge `ref` into it, then create the record with one initial `Registration`. *(open: when do we coalesce vs reuse? probably "always coalesce on ServiceKey identity, but each Registration carries its own release-callback").*
  3. Mount a release-callback impl on the per-connection scope. Return `(short_id, release_ref)` to the caller. Releasing (via `release()` or by the bridge's token ending) removes the `Registration` from the record; if the record's tracker count hits zero, set the worker task's `shutdown_event` to drain the scope.
- **`copy_entity(name, entity)`**:
  1. `await entities_scope.copy_from_scope(entity)` to bring the entity into the workstation's scope.
  2. Allocate / look up the entity short ID.
  3. Create or augment a `TrackedEntityRecord`; append a `Registration`.
  4. Mount a release-callback. Releasing removes the registration; when no trackers remain, `entities_scope.abandon(entity)`.
  5. Return `(short_id, release_ref)`.
- **`get_service(short_id)`**: look up the record; raise `IRException` if no active record. Lift the workstation's `bridge_ref` into the *calling* RPC connection's user scope; return the resulting `ServiceRef`. Annotation: `OverrideThisTypeBeforeUsing`.
- **`get_service_by_external_name(name)`**: same, but rejects if no active registration currently has this `external_name`.
- **`get_entity(short_id)`** / **`get_entity_by_external_name(name)`**: similar; return the `FxtrID`. The actual entity data is reachable via the calling connection's user scope through `entities_scope`'s inheritance bridge — see scope wiring.
- **`name_connection(name)`**: write through to `RPCConnectionRecord.name`.

All methods take the state lock for bookkeeping; long async steps (e.g. `copy_from_scope` for entities) happen outside the lock.

## Scope wiring (initial sketch — to iterate on)

```
root_scope (mountable, owned by WorkstationServer)
├── entities_scope (mountable, isolated=False from root)        # tracked entities live here
├── service_scope_<service_key_n> (isolated, mountable)         # one per registered service
└── rpc_endpoint per-connection scope (a child of root)
     ├── open_rpc_scope's host_scope + user_scope (with include=[entities_scope])
     └── per-connection impl-mount scope (where WorkstationServiceImpl is mounted)
```

The `/rpc` connection handler:

1. Allocates an `RPCConnectionRecord`.
2. Opens a per-connection scope under `state.root_scope`.
3. Mounts a `WorkstationServiceImpl` on that scope.
4. Calls `open_rpc_scope(transport, our_main=workstation_ref, include=[state.entities_scope], remote_service_type=NoOpService)`.
5. `open_rpc_scope`'s body is just `pass` — the connection naturally stays open as long as the peer holds our main service alive. When the peer disconnects (or shutdown fires), the conn drains, the body returns, and we tear down the per-connection scope on the way out.

*The exact nesting of "per-connection impl-mount scope" vs "open_rpc_scope's host_scope" is an open question; the impl needs to be reachable through the host_scope precondition lookup. Probably it lives inside an `include` we pass to `open_rpc_scope`.*

## Cleanup on close

*Open question — needs care.* The hard cases:

- WS disconnects mid-call: `open_rpc_scope`'s scope drains, the impl method's task is cancelled, registrations belonging to this connection unwind via their release-callback's `on_end`.
- Server shutdown while RPC connections hold registrations: shutdown trigger wakes hypercorn → individual `rpc_endpoint`s see their websocket close → each tears down independently.
- A peer holding a `get_service` ref alive when its source registration is released: handled by the per-record worker task — we drop the registration, but the bridge keeps the underlying handle alive on the peer's side. *(see "Drained-but-alive: deferred" below.)*
- A peer disconnecting while another peer still holds a service it pulled via `get_service`: same as above.

We'll exercise these cases as we implement and fold what we learn back into this section.

## Drained-but-alive: deferred

The earlier draft of this plan tried to surface "service is no longer tracked but the underlying lease hasn't drained yet" as workstation state. After more thought, this probably doesn't work at the lease layer the way we want — the lifetime of a pulled service may be tracked directly through lease-passing from the source RPC connection, which means the workstation's per-record `service_scope` will drain immediately when the registration drops, even though the peer still has a working ref.

Properly observing "still alive elsewhere" likely needs the workstation to mount its own *proxy* service that forwards calls back to the original — that gives the workstation a lifetime + identity it controls. Out of scope for stage 1; can revisit if we decide we want the debug UI to show this.

For stage 1 we drop the feature entirely and treat a registration release as fire-and-forget cleanup of the corresponding bookkeeping.

## WebSocket transport adapter

`_ws_transport.py` provides:

```python
@asynccontextmanager
async def asgi_websocket_object_stream(websocket) -> AsyncIterator[ObjectStream[JSONTypes]]:
    # Wraps a Starlette WebSocket as an ObjectStream:
    # send: dumps JSON + websocket.send_text(...)
    # receive: websocket.receive_text() + json.loads; on close, raises EndOfStream
    ...
```

We adopt **Starlette** for routing + the websocket abstraction. Starlette is already in our deps, gives us a clean `WebSocketRoute` + `WebSocket.accept/receive_text/send_text/close`, and plays well with hypercorn.

`ws_app.py`'s `/rpc` route:

```python
async def rpc_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    state = websocket.app.state.workstation_state
    async with state.add_rpc_connection(websocket) as record:
        async with FxtrScope(mountable=True, name=f"rpc-conn/{record.connection_id}") as conn_scope:
            impl = WorkstationServiceImpl(state, record)
            workstation_ref = conn_scope.mount(impl)
            async with asgi_websocket_object_stream(websocket) as raw:
                transport = wrap_json_stream(raw)
                async with open_rpc_scope(
                    transport, workstation_ref,
                    include=[state.entities_scope, conn_scope],
                    remote_service_type=NoOpService,
                    name=f"workstation-rpc/{record.connection_id}",
                ):
                    pass   # peer keeps the conn alive until it disconnects
```

## ASGI app + hypercorn

Starlette wires routes:

```python
def build_app(state: WorkstationState) -> Starlette:
    app = Starlette(routes=[
        WebSocketRoute("/rpc", rpc_endpoint),
        # /client lands here in stage 2
    ])
    app.state.workstation_state = state
    return app
```

`server.py` runs hypercorn on the workstation's task group:

```python
class WorkstationServer:
    async def __aenter__(self):
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        self.state = await self._stack.enter_async_context(_make_state(...))
        self._shutdown_event = anyio.Event()
        self._tg = await self._stack.enter_async_context(create_task_group())
        config = hypercorn.Config()
        config.bind = [f"{self.host}:{self.port}"]
        async def _run():
            await hypercorn.asyncio.serve(
                build_app(self.state),
                config,
                shutdown_trigger=self._shutdown_event.wait,
            )
        self._tg.start_soon(_run)
        await self._wait_for_bound()
        return self
    async def __aexit__(self, *exc):
        self._shutdown_event.set()
        await self._stack.__aexit__(*exc)
```

*The "discover the bound port" path is mildly fiddly with hypercorn; we'll figure out the cleanest hook in implementation.*

## Tests (deferred until the design settles)

Tests are deliberately the *last* implementation step. The ordering below tries to keep the design feeling solid as it emerges; once the pieces fit, we lock the behavior in with tests.

When we get there, the test inventory will be roughly:

- `conftest.py` — pytest fixtures: `workstation_server`, `connect_rpc(url)` (httpx-ws + `wrap_json_stream` + `open_rpc_scope`).
- `test_server_lifecycle.py` — server starts and stops, `bound_url` resolves.
- `test_rpc_main_service.py` — single-connection coverage of every method.
- `test_cross_connection.py` — the keystone test: A registers, B retrieves; releases on A invalidate B's name lookups but keep B's already-pulled ref live.
- `test_short_id_stability.py` — register / release / re-register returns the same short ID.
- `test_bounceback.py` — A registers, B retrieves, B re-registers; the workstation has one record with two registrations.

We'll add `httpx-ws` to dev deps when we get to writing tests.

## Implementation steps

We're feeling out the design as we go, so the steps below are intentionally small, sequential, and reviewable one-by-one. Tests come at the end.

- [x] **Walking skeleton.** `WorkstationServer` async ctx manager runs hypercorn on a Starlette app with a single `/rpc` route that accepts and immediately closes. Ephemeral-port reservation via pre-bind/release; "wait until listening" via TCP connect-poll. Lifecycle tests cover start/stop and TCP accept (`tests/test_server_lifecycle.py`).
- [x] **`open_rpc_scope` over the websocket.** `_ws_transport.asgi_websocket_object_stream` wraps a Starlette WebSocket as `ObjectStream[JSONTypes]` (text frames carry one JSON message; `WebSocketDisconnect` → `EndOfStream`; `send_eof` closes the WS). `/rpc` opens `open_rpc_scope` with `our_main_service=None` (defaults to `NoOpService`) and `remote_service_type=UnknownService`; body is a no-op. A test-only `connect_rpc` helper in `tests/_ws_client.py` mirrors the adapter on the httpx-ws side. Smoke test (`test_rpc_bootstrap_round_trip`) confirms the bootstrap and clean teardown.
- [x] **Workstation state skeleton.** `state.py` defines `WorkstationState` (dict of RPC connections + an `anyio.Lock`) and `RPCConnectionRecord` (UUID, peer_addr, connected_at, name). `state.add_rpc_connection(peer_addr)` is an async CM that creates / removes the record. `WorkstationServer` constructs one `WorkstationState` per CM entry and attaches it to the Starlette app via `app.state.workstation_state`; the `/rpc` route reads it and wraps the `open_rpc_scope` body inside `add_rpc_connection`. Tests verify single + two-concurrent connection lifecycles.
- [x] **WorkstationServiceProto + Impl skeleton + `name_connection`.** `protocols.py` defines `WORKSTATION_NS`, `WORKSTATION_SERVICE_TYPE_ID`, and `WorkstationServiceProto` with one method (`name_connection(name)`). `service_impl.py` defines `WorkstationServiceImpl(state, record)` extending `TypedServiceImpl`. `/rpc` opens a per-connection `FxtrScope(mountable=True)`, mounts the impl, and passes its ref as `our_main_service` to `open_rpc_scope`. Test exercises the round-trip through `replace_service_type(WorkstationServiceProto).resolve()`.
- [x] **`register_service` / `get_service` (id-only first, no by-name).** Brought up `TrackedServiceRecord` + per-record worker task (`service_record_worker`) + `Registration` model in `state.py`, with `ShortIDAllocator` in `short_ids.py`. `register_service` / `get_service` impls in `service_impl.py`. Cross-connection round-trip exercised by `test_register_and_get_service_across_connections_explicit_release` and `test_register_and_get_service_across_connections_inner_scope` in `tests/test_server_lifecycle.py`. Note: dropped the `diag_mint_witness` diagnostic and `test_diag_witness_pattern` after the underlying fxtr2 future-box race was fixed (commit `vozxswzt`).
  - **Auto-release-on-drop has a structural cycle when the holder owns the registered ref.** `service_record_worker` bridges `source_ref` from the dispatch `call_scope` into a workstation-owned `service_scope` — and `bridge_service_from_scope` documents that the bridge pins the source scope until the bridge is released. The bridge is released when `service_scope` drains, which only happens once `shutdown_event` is set, which only happens when `call_scope`'s end-callback fires, which only fires when `call_scope` drains. If the holder of the chain-bridged `release_cb_ref` is the same scope (or an ancestor) of the scope that owns the registered service, the holder can't drain (waiting for the workstation to release the lease on the registered ref), so the cycle never breaks. Two structurally safe usage patterns exist; both are covered by tests:
    1. Call `release_cb_ref.release()` explicitly before tearing down — short-circuits the cycle by setting `shutdown_event` directly.
    2. Resolve the workstation ref into a *child* scope of the scope that owns the registered ref, so the chain-bridged `release_cb_ref` lives in a shorter-lived inner scope. Exiting the inner scope releases the registration while the outer scope is still alive to receive the lease-end for the registered service.
  - The `register_service` docstring documents both patterns. Future work could try to break the cycle structurally (e.g. lifting the input ref to `record.scope` so `service_scope` can bridge from there instead) but this is non-trivial — `bridge_service_from_scope` always pins its source.
- [x] **External names for services + `get_service_by_external_name`.** Added `service_external_names: dict[str, Registration]` on `WorkstationState` (last-writer-wins); `register_service` populates it; `release_registration` clears the entry only if it still points at the released registration. New `get_service_by_external_name` shares a `_bridge_tracked_into_call_scope` helper with `get_service`. Also pulled forward: `ShortIDAllocator.forget_service` (called from a token-end callback registered on first allocation) so the short-ID mapping is dropped when the underlying handle's lifetime ends. New `tests/test_service_registration.py` covers re-registration of the same handle, bounceback, release-while-consumer-holds, consumer-takes-over (uses external-name lookup), and connection-close → short-ID-forgotten. Test-only fixtures (`EchoImpl`, `EchoProto`) moved to `tests/_fixtures.py`.
- [x] **`copy_entity` / `get_entity` + by-name variants.** Added `entities_scope` (workstation-wide isolated scope) opened by `WorkstationServer` alongside `root_scope`; `add_rpc_connection` now `include`s both so the per-connection user_scope sees tracked entities. New `TrackedEntityRecord` / `EntityRegistration` / `release_entity_registration` mirror the service shapes; `ShortIDAllocator` got `get_or_assign_entity` / `lookup_active_entity` / `deactivate_entity` (id-string prefix, `entity_short_id_prefix_len` chars for `d`/`c`/`n`-tagged IDs, extended on collision; full id-string for other tags). `copy_entity` does `entities_scope.copy_from_scope(entity, call_scope)` then registers, with the same exclusive-name rule as services. `get_entity` / `get_entity_by_external_name` return the FxtrID; consumers `load()` through the inheritance bridge. New `expose_entity` helper mirrors `expose_service`. On last-release the workstation `abandon`s the entity from `entities_scope`; consumers that retrieved the FxtrID via `get_entity` keep working because the RPC layer bridges each returned entity's lifetime token across the wire (so the consumer's connection holds an independent claim). New `tests/test_entity_registration.py` covers cross-connection round-trip, expose-helper round-trip, exclusive-name rejection, short-id format, and post-release loadability.
- [x] **Release callback wiring.** `ReleaseCallbackImpl` (in `service_impl.py`) handles both kinds via mutually-exclusive `registration` / `entity_registration` kwargs. Both `register_service` and `copy_entity` register an end-callback on `call_scope._token` so dropping the chain-bridged ref auto-releases. Idempotency via the `released` flag on `Registration` / `EntityRegistration`. Bounceback exercised by `test_bounceback_returns_same_short_id_in_both_connections` and `test_consumer_takes_over_via_reregistration`. Simultaneous-release / registration-after-release covered by `test_crash_scenarios.py` (the explicit `release()` in `expose_*`'s finally happens *and* the dispatch scope drains for the same registration; both paths are no-ops by virtue of `released` and `forget_service` `ValueError` swallowing).
- [x] **Cleanup on disconnect / shutdown.** `add_rpc_connection`'s `finally` sweeps both `record.registrations` and `record.entity_registrations`, calling the appropriate `release_*` and emitting a `_log.warning` if anything was actually still on the list (so a missed teardown path surfaces). Server shutdown drain order: `serve_tg` (hypercorn drains, each rpc_endpoint unwinds its `open_rpc_scope` → `add_rpc_connection.finally` runs) → `background_tg` (per-service-record workers exit once their `shutdown_event`s fire) → `entities_scope` → `root_scope`. Verified end-to-end by every test exiting `WorkstationServer` cleanly. The crash-scenarios tests additionally exercise the disconnect path under exceptional unwinding. The known limitation — auto-release-on-drop cycle when caller doesn't use `expose_*` — is documented in `notes/service_lifecycle.md` and the `register_service` docstring.
- [x] **Update CODE_OVERVIEW.md** reflecting the new module layout and concepts. Updated incrementally as each subsystem landed; final pass after the entity work.
- [x] **Tests.** No `conftest.py` ended up needed — `tests/_fixtures.py` (shared impls/entities) and `tests/_ws_client.py` (test-only `connect_rpc` helper) carry the shared bits, and each test inlines its own `async with WorkstationServer() as ws:` setup. Test files: `test_smoke.py`, `test_server_lifecycle.py`, `test_service_registration.py`, `test_expose_service.py`, `test_entity_registration.py`, `test_crash_scenarios.py`. 29 tests total; stable across stress runs.
- [x] **Update CODE_OVERVIEW.md** for anything that drifted during testing. Final pass done.

Mid-implementation, expect to bounce back to update earlier sections of this plan as the design clarifies.
