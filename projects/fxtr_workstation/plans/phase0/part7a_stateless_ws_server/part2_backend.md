# Part 2 — Python service-layer backend library

Extend `v2/fxtr2/experimental/http/` with the **service** layer (codec,
registry/discovery, REST + ws invocation endpoints) on top of the
existing entity endpoints. **Python only**; the TS backend is deferred
(overview §"Backend libraries"). No rename/promotion out of
`experimental/` (Q-N).

**Scope (trimmed):** Part 2 ships the *library* + Python unit/endpoint
tests only. The kitchen-sink **tester script is deferred to Part 3**
(it's the integration-test backend, lives with the connector-http
tests, and exists to give every *connector-level* method coverage —
not to be a realistic app). No dynamic-dispatch backend here: a
dynamic dispatcher is just one consumer of connector methods (resolve
= an ordinary invoke, renderer = an ordinary entity load), so it's
covered transitively in Part 3, not by building a dispatch service.

Design rationale + the protocol it implements:
[`design/02_server_backends.md`](design/02_server_backends.md) and
[`design/01_wire_protocol.md`](design/01_wire_protocol.md).

## Re-orientation pointers

| What | Where |
|---|---|
| Existing entity endpoints (`/data`, `/pin`, `/pins`, `/preview`) | `v2/fxtr2/experimental/http/asgi_app.py:31-154` (`create_fxtr_app(scope, pins)`) |
| Server runners (hypercorn) | `v2/fxtr2/experimental/http/server.py` |
| ws endpoint shape to mirror | `projects/fxtr_workstation/src/fxtr_workstation/client_app.py:75-154` (accept → task group → read/write sibling loops) |
| Escape codec model | `projects/fxtr_workstation/src/fxtr_workstation/wire_escape.py` |
| Entity/type creation patterns (for tests) | `system_entities.define_type(ns, name)`, `DataclassEntity(fxtr_type=...)`, `.store(scope)` (cf. `_reload_harness_workstation.py:54-94,182-184`) |

## Where the code goes

New modules under `v2/fxtr2/experimental/http/` (named `service_*`
since they're the service layer over the existing entity endpoints):
`service_codec.py`, `service_registry.py`, `service_rest_endpoint.py`,
`service_ws_endpoint.py`. `Cache-Control: public, immutable` on
`/data/{id}` responses (Q-7) if not already present. Run tests with the
**v2 venv** (`uv run pytest` from `v2/`).

## Steps

### A. Codec (`service_codec.py`) — resolved-mode escape ✅
- [x] `unescape_value` / `unescape_args(payload) -> (args, kwargs)`:
  list-walk mirroring `wire_escape.unescape`. `["entity", id_string]` →
  `FxtrID.from_id_string` (**parse only**; bad id → `ServiceCodecError`);
  `["service", descriptor]` → `ServiceDescriptor.from_wire`;
  `["callback", uuid]` → `CallbackAddress`; `["bytes", b64]` → bytes.
  **Deviation from design/01**: unescape does *not* take a `scope` /
  verify deep-fetchability — it's sync + pure; an unknown id just fails
  when the impl loads it. (Flagged below.)
- [x] `escape_value(value, *, register_transient=None) -> JSON`:
  `FxtrID` / `EntityRef` / `Raw*FxtrID` → `["entity", id_string]`;
  `ServiceDescriptor` → `["service", descriptor]` (incl.
  `service_type_id`); `TransientService` (ws only) → `register_transient`
  → `["service", {kind:"transient", id}]`; bytes → base64; lists
  `[[...]]`.
- [x] Value types: `ServiceDescriptor` (kind/url/method/name/id +
  optional `service_type_id`, with `to_wire`/`from_wire`),
  `CallbackAddress(uuid)`, `TransientService(impl)`. No `error_type`
  (Q-K); `ServiceCodecError` for malformed input.
- [x] Unit tests (`test_service_codec.py`, 29 cases): primitives /
  containers, entity round-trip + nested, service descriptor (http/ws/
  transient) round-trip + `service_type_id` preserved + invalid-kind
  reject, callback → address, bytes round-trip + bad-base64 reject,
  transient requires `register_transient`, `unescape_args` shapes.

### B. Registry + discovery (`service_registry.py`) ✅
- [x] `ServiceRegistry(scope, *, base_url=None)` with
  `add_entity(name, fxtr_id)`, `add_rest_service(name, *, path, impl,
  method="POST", type_id=None)`, `add_ws_service(name, *, ws_path,
  impl, type_id=None)`. Duplicate service/entity names rejected.
- [x] `routes()` → REST `Route`s + one `WebSocketRoute` per ws path
  (composing the C/D endpoints); `discovery()` / `discovery_handler` →
  `{entities, services}` with each descriptor carrying
  `service_type_id`. Descriptor urls relative unless `base_url` set
  (then absolute; ws → ws://). `create_fxtr_app` untouched.
- [x] Unit tests (`test_service_registry.py`, 5 cases): discovery
  shape (entity + http/ws descriptors, type_id present/omitted),
  routes() emits REST+ws, duplicate-name rejection, absolute base_url,
  end-to-end `/discovery` + a mounted REST invoke.

### C. REST invocation endpoint (`service_rest_endpoint.py`) ✅
- [x] `rest_service_route(path, impl, *, scope, method="POST") -> Route`
  (+ `make_rest_handler`). `impl` is the shared `ServiceImpl` shape
  (callable or `{method: fn}` map). Parses `GET
  ?method&payload=<urlencoded JSON {args,kwargs}>` / `POST
  {method,args,kwargs}`, runs `unescape_args` / `escape_value`, body =
  escaped result. Errors → `{message}`: malformed/codec/unknown-method
  → 400, impl exception → 500. Shared dispatch (`invoke_impl`,
  `ServiceImpl`, `UnknownServiceMethodError`) lives in a new small
  `service_dispatch.py` (so REST + ws don't duplicate it; codec stays
  pure-wire).
- [x] `RestServiceContext(scope, request)` — no `notify`/`transient`
  (callbacks inert over http; a `["callback", uuid]` arg decodes to a
  `CallbackAddress` the impl ignores).
- [x] `$fxtr.type` is just a method an impl may handle — no special
  framework handling.
- [x] Unit tests (`test_service_rest_endpoint.py`, 11 cases, httpx
  ASGITransport): GET echo (primitive + entity round-trip + kwargs),
  `$fxtr.type` → entity tag, missing/unknown method + bad payload →
  400; POST returns-entity, impl failure → 500 `{message}`, callback
  arg inert, non-object body → 400.

### D. ws invocation endpoint (`service_ws_endpoint.py`) ✅
- [x] `ws_services_endpoint({name: impl}, *, scope)` → ASGI handler:
  accept → anyio task group with read-loop + write-queue siblings
  (mirrors `client_app.py`), per-invoke task spawn.
- [x] Per-connection `_ConnectionTable`: inbox uuids (collected from
  `CallbackAddress`es in unescaped args) + transient services (id →
  impl). Dies with the connection.
- [x] Frame handling: `{type:"invoke", id, service, method, args,
  kwargs}` → resolve `service` (name or transient id; unknown →
  `error`), dispatch, reply `result` / `error`.
- [x] `WsServiceContext`: `notify(addr, payload)` (emits `notify`
  frame for a known inbox uuid, else silent drop) + `transient(impl)`.
- [x] No timeouts; ws FIFO ordering; no ping (Q-O).
- [x] Unit tests (`test_service_ws_endpoint.py`, 7 cases, fake-WS
  driving the ASGI handler in-process): invoke happy + error, unknown
  service, ordered-notifies-before-result, notify to unknown inbox
  dropped, transient subscribe→invoke, malformed JSON.

Update CODE_OVERVIEW.md ✅ — `v2/CODE_OVERVIEW.md` + the `experimental/`
`LOCAL_CODE_OVERVIEW.md` updated with the `service_*` layer.

## Deferred to Part 3

- The **kitchen-sink tester script** (the integration-test backend):
  pins a nontrivial entity graph, exposes GET/POST/WS services that
  return those entities + descriptors + callbacks/transients + a
  `$fxtr.type`-implementing service — built in the connector-http test
  dir and spawned over the wire by Part 3's TS integration tests, so
  every connector-level method gets coverage.
- The cross-backend smoke test returns with the deferred TS backend.

Update CODE_OVERVIEW.md (final).
