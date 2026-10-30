# 02 — Server backend library (Python; TypeScript deferred)

Status: resolved. **This change builds the Python backend only.** The
TypeScript backend (Hono) is **deferred to a follow-up** — the
existing TS port is already partial and some of the TS logic it would
have leaned on has had a change of plans. The TS-specific notes below
are kept for when it lands but marked **[deferred]**.

Implements the protocol in [`01_wire_protocol.md`](01_wire_protocol.md).
"Feel like Starlette plus helpers" — the library is codec + adapters,
not a framework.

## Re-orientation pointers

| What | Where |
|---|---|
| Python entity endpoints (exist) | `v2/fxtr2/experimental/http/asgi_app.py:31-154` (`create_fxtr_app(scope, pins)`, routes `:142-147`, CORS `:149-151`) |
| Python server runners (exist) | `v2/fxtr2/experimental/http/server.py:25-124` (`serve_until` / `serve_forever` / `serving_context`, hypercorn) |
| TS entity endpoints (exist) | `v2/ts/packages/fxtr-http-server/src/server.ts:114-260` (`createFxtrApp(scope, pins)`, Hono, CORS `:119`) |
| Starlette ws endpoint pattern to mirror | `projects/fxtr_workstation/src/fxtr_workstation/client_app.py:75-154` (accept → task group → `_read_loop`/`_write_loop` siblings), `ws_app.py:57-79` |
| Escape codec model | `projects/fxtr_workstation/src/fxtr_workstation/wire_escape.py:65-154` |
| Python tester-script precedent | `frontend_lib/packages/fxtr-ui-connector-workstation/tests/_demo_workstation.py` + `_integration_harness.ts` |

## Where the code lives

Extend `fxtr2.experimental.http` **in place** (Python). The
`experimental.http` → `fxtr2.http` rename (and the read-namespace
client → `fxtr2.data` move) is **deferred to a follow-up** (resolved
Q-N) — doing it here would conflict with parallel work, and it buys
nothing for this change.

**[deferred]** TS stays `@fxtr/http-server` / `@fxtr/http-client`
(already non-experimental names); the TS build-out lands with the
follow-up.

## New modules (Python; `*.ts` twins **[deferred]**)

### 1. Codec (`service_codec.py`; `service_codec.ts` **[deferred]**)

Resolved-mode escape/unescape (01 §"Escape codec"):

- `unescape_args(payload, *, scope) -> (args, kwargs)` — entity tags
  → `FxtrID` validated against `scope` (parse error / unknown id →
  typed request error → 4xx or ws `error` frame); service tags →
  `ServiceDescriptor` value; callback tags → `CallbackAddress`
  (opaque holder of the uuid).
- `escape_result(value, *, ...) -> JSON` — `FxtrID` / `EntityRef` /
  `Raw*FxtrID` → `["entity", id_string]` (mirror the unwrapping in
  `wire_escape.py:94-100`); `ServiceDescriptor` → service tag;
  `TransientService` (ws ctx only) → registers in the connection
  table and emits `{kind: "transient", id}`; bytes → base64.

Likely shares list-walk scaffolding with `wire_escape.py` /
`escape.ts`; **TODO** decide share-vs-copy when writing it (the
identifier-payload swap is small; the walk is ~100 lines either
way).

### 2. Service registry + discovery (`service_registry.py`; `service_registry.ts` **[deferred]**)

A small builder so discovery can't drift from mounted routes:

```
reg = ServiceRegistry(base)
reg.add_http_service("render", path="/svc/render", method="GET", impl)
reg.add_ws_service("scheduler", ws_path="/ws", impl)
reg.add_entity("demo/root", fxtr_id)          # optional sugar over pins
app.routes += reg.routes(); app.route("/discovery", reg.discovery_handler)
```

The discovery *content* remains entirely the author's choice
(open_questions #1, answered): the registry is convenience, not
policy — authors can also hand-write the discovery response, list a
subset, list extra names, or alias one service under several names.

Each listed service descriptor carries its **`service_type_id`**
annotation (resolved Q-H) — the type entity's `id_string`. The
registry captures it when a service is added (the author passes the
type id, or the helper derives it from the impl's declared type),
and the codec stamps it onto descriptors returned from invokes too,
so the connector always learns a service's type without an extra
`$fxtr.type` round trip for record/metadata display.

### 3. REST invocation endpoint (`service_rest_endpoint.py`)

`impl` is `async (method, args, kwargs, ctx) -> value` or a
`{method_name: fn}` map. The adapter produces a Starlette `Route`
(Hono handler **[deferred]**) that parses GET-query/POST-body per 01,
runs the codec, maps exceptions → non-2xx JSON `{message}` (no
`error_type` — resolved
Q-K, matching the workstation). `ctx` for http services carries the
scope + request info; its `notify` is absent (callbacks are inert
over http).

### 4. ws endpoint (`service_ws_endpoint.py`)

`ws_services_endpoint({name: impl})` → one endpoint owning:

- accept + read loop / write queue (mirror
  `client_app.py:101-154`'s sibling-loops shape, minus all
  subscription/session dispatch).
- per-connection table (inbox routes + transient services, 01).
- per-invoke task spawn; impl exception → `error` frame.
- `ctx.notify(callback_address, payload)` and
  `ctx.transient(service_obj) -> TransientService` for impls.

Python: plain Starlette `WebSocketRoute` + anyio task group, like the
workstation.

**[deferred]** TS: no server-side ws exists in the workspace today —
when the TS backend lands, use **`@hono/node-ws`**
(+ `@hono/node-server`, resolved Q-M), since the entity app is already
Hono and `@hono/node-server` is needed to serve it anyway.

## Tester script — built in Part 3

The kitchen-sink tester (the integration-test backend) is **deferred to
Part 3**, where it's actually used: it lives with the connector-http
tests and is spawned over the wire by the TS integration tests (the
`_demo_workstation.py` + `_integration_harness.ts` pattern). Its job is
to give every *connector-level* method coverage — not to be a realistic
app — so it just exposes every route type built here:

- pinned entities in a **nontrivial graph** (a parent referencing
  children) so loads + child-ref decoding are exercised;
- `echo`-style GET service and a POST service returning entities +
  an undiscoverable http service descriptor;
- a ws service with `subscribe(handle) -> transient unsubscribe`,
  `notify_n(handle, n, prefix)` (ordered notifies before result,
  mirroring `_demo_workstation.py`'s `notify_n`), and `fail(message)`;
- a service implementing `$fxtr.type` (so the metadata / type-discovery
  connector path is covered).

No dynamic-dispatch backend: a dynamic dispatcher is just one consumer
of connector methods (resolve = an invoke, renderer = an entity load),
so it's covered transitively. (The cross-backend smoke test that diffs
two backends' transcripts returns when the **[deferred]** TS backend
lands.)

## Out of scope / notes

- Auth: none designed; CORS stays permissive like the existing apps.
  Deployments wanting auth put it in front (reverse proxy).
- `Cache-Control: public, immutable` added to `/data/{id}` responses
  (open_questions #7, agreed).
- The backend never initiates entity fetches from clients; there is
  no "upload" surface at all in part 7.
