# 01 — Wire protocol: identifiers, codec, HTTP + ws encodings

Status: resolved. Part of [`../overview.md`](../overview.md); the
[`../open_questions.md`](../open_questions.md) decision record carries
the final landings (referenced inline below).

## Re-orientation pointers

| What | Where |
|---|---|
| Python escape codec (the model) | `projects/fxtr_workstation/src/fxtr_workstation/wire_escape.py:46-58` (tags), `:65-154` (escape/unescape) |
| TS escape codec (frontend twin) | `frontend_lib/packages/fxtr-ui-connector-workstation/src/escape.ts:71-111` (tags), `:184-364` (escapeWire/unescapeWire), `:567-597` (escapeInvokeArgs) |
| Workstation client protocol (structural inspiration for ws RPC) | `projects/fxtr_workstation/src/fxtr_workstation/client_protocol.py` |
| id_string format | `v2/fxtr2/data/entity_core.py:336-416` (`FxtrID.id_string` / `from_id_string`); TS twin in `v2/ts/packages/fxtr/src/core/entityCore.ts` |

## Identities

- **Entity**: full `id_string` — always a **bare local** id on the
  wire (the debug-only `@`-provenance suffix is rejected, see
  "Provenance on the wire" below). The server can serve any id reachable
  through its scope (`/data`); the client may send only ids the
  server already knows (the server can never fetch client data). A
  *layered* (data, prov) id carries its provenance **explicitly
  alongside** the data id (a third escape element / a tuple pin value —
  see "Provenance on the wire"), never folded into one string.
- **Service descriptor** (JSON object):
  - `{kind: "http", url, method: "GET" | "POST"}`
  - `{kind: "ws", url, name}` — `name` scoped to the ws endpoint at
    `url`; all `ws` services with the same `url` multiplex one
    connection.
  - `{kind: "transient", id}` — connection-scoped, minted by the
    server *during* a ws exchange (see below). Has no `url`: it is
    only meaningful on the connection that delivered it.
  - Any descriptor may carry an optional **`service_type_id`** field
    (the type entity's `id_string`), set by the server wherever it
    introduces a service (discovery + invoke results). It mirrors the
    workstation, which knows a service's type eagerly at registration.
    The connector reads it to fill records and answer
    `allocateGetServiceMetadata` locally (design/03 §Metadata). It is
    a **non-identity annotation** — see canonicalization below.
  - `url`s may be relative (resolved against the connector's base
    URL) or absolute. **Canonicalization for descriptor hashing**
    (shortId allocation, resolved Q-J): resolve `url` to absolute,
    drop the `service_type_id` annotation, sort keys,
    JSON-stringify. Two descriptors that differ only in
    `service_type_id` therefore hash to the same shortId.
- **Callback / inbox**: client-minted UUID identifying one inbox of
  one client session.

## Escape codec — two modes

Same list-based escape family as today (`[[...]]` list wrapping,
tagged leaves), with two encodings of ref leaves:

| Tag | Server wire (resolved mode) | Persistence (shortId mode) |
|---|---|---|
| `["entity", X]` | X = id_string | X = client shortId |
| `["service", X]` | X = descriptor object | X = client shortId |
| `["callback", X]` | X = inbox UUID | X = slot int (workstation form) |
| `["bytes", b64]` | same | same |
| `["op_result", slot]`, `["inbox", slot, offset]`, `["broken_*", …]` | never on server wire | client-only, as today |

ShortId mode **is** today's frontend codec (`escape.ts`) unchanged —
persistence blobs look exactly like workstation wire state. Resolved
mode is a new pair of encode/lookup callbacks supplied by the
connector (translate stub ↔ id_string/descriptor through the shortId
table). The server side implements only resolved mode and only the
four server-wire tags.

Server-side unescape resolves `["entity", id_string]` against its
scope (parse via `FxtrID.from_id_string(..., allow_provenance=False)`,
verify deep-fetchable; unknown → request error), passes descriptors through as
typed values, and turns `["callback", uuid]` into an opaque
`CallbackAddress` the service impl can hold or ignore (only the ws
endpoint can actually deliver to it).

## Provenance on the wire

A layered fxtr id is `(data, prov)` — a data id plus a provenance-node
parent. Provenance rides **explicitly alongside** the data id; the data
slot is always a bare data id. (Full design + rationale:
[`../part6_http_provenance.md`](../part6_http_provenance.md).)

- **Entity escape.** Bare `["entity", "<data>"]`; layered
  `["entity", "<data>", "<prov>"]` (a third element). The decoder
  branches on **arity** — exactly 2 or 3, never longer. `Raw*FxtrID`
  (CBOR-loaded refs) are always bare, since provenance is never in CBOR.
- **Pin / discovery-entity value.** A tag-less value: bare
  `"<data>"` (string) vs layered `["<data>", "<prov>"]` (array). Used by
  `/pins`, `/pin`'s `id` field, and `/discovery` entity entries. Decode:
  string ⇒ bare, array ⇒ layered.
- **`/pin` precomp.** For a layered pin, `/pin` bundles the **prov
  node's** precomp closure (seed the existing closure on the prov node,
  whose `unwraps_to` pulls in the data node) — so the client can unwrap
  the prov node without an extra `/data` fetch. `/pins` + service results
  carry no precomp (the client `/data`-fetches the prov node on demand).
  `/data/{id}` itself is unchanged — by-id, bare.
- **The `provenanced` server gate.** Emission of the layered forms is a
  server-side opt-in (`provenanced` on `create_fxtr_app` /
  `ServiceRegistry` / the service endpoints, default `False`). When off,
  every id emits bare even if layered — so a server never leaks
  provenance unintentionally. **Decode is never gated** (a layered value
  is always accepted).
- **`@`-form invariant.** The single-string `data@prov` form
  (`id_string(include_provenance=True)`) is debug-only and **must never
  appear on the wire** — every id_string is parsed through a bare-id
  guard that rejects `@` (it would otherwise be silently parsed into a
  layered id).

## Simple (HTTP) service invocation

One route per service, fixed at server construction.

- `GET {url}?method=<name>&payload=<urlencoded JSON>` where payload
  is `{args: [...], kwargs: {...}}` in resolved-mode escape.
  Deterministic services get CDN caching for free. Documented
  constraint: keep GET args small (no automatic POST fallback).
- `POST {url}` body `{method, args, kwargs}` (JSON, resolved mode).
- Success: `200` with a JSON body holding the resolved-mode-escaped
  result. Results may carry entity id_strings (fetchable via
  `/data`) and service descriptors — including descriptors for
  routes not listed in discovery ("undiscoverable but valid").
- Error: non-2xx with JSON `{message: string}`. Client maps to an
  errored fill, exactly like a workstation `invoke_error` event. **No
  `error_type`** (resolved Q-K): the workstation carries none —
  invoke/load/metadata errors are `message`-string-only — so we match
  it. (Its one structured error enum, `MutationRejectReason`, is
  mutation-pipeline-specific and has no stateless analogue.)
- Callback args are legal but inert: the server has no channel back;
  a cooperative service just ignores them.
- Services participating in dynamic dispatch implement the
  `$fxtr.type` method (zero args → their type entity ref), matching
  the workstation method convention
  (`fxtr-ui-react-dispatch/src/dynamic_dispatcher.ts:1239-1243`).

## ws RPC

One JSON object per text frame.

```
client → server:
  {type: "invoke", id, service, method, args, kwargs}
server → client:
  {type: "result", id, value}
  {type: "error",  id, message}
  {type: "notify", inbox, payload}
```

- `id`: client-minted, opaque to the server, referenced exactly once
  by the terminal `result`/`error`. `notify` frames may interleave
  before/after the terminal frame (long-lived subscriptions keep
  notifying indefinitely).
- `service`: either a `name` (from a `{kind: "ws"}` descriptor whose
  `url` is this connection) or a transient `id` previously handed
  out **on this connection**. Unknown service/method → `error`.
- `args`/`kwargs`/`value`/`payload`: resolved-mode escape.
- No request timeouts. No client-side ordering guarantees beyond ws
  FIFO — ordering is the server's business, same as the
  server-session case. Framework-level ping/keepalive is **skipped**
  until a proxy forces it (resolved Q-O).
- Each invoke runs as its own server task (no head-of-line blocking;
  matches workstation expectations).

### The per-connection table

The only server-side state. Holds, per live ws connection:

1. **Inbox routes**: every inbox UUID seen in a `["callback", uuid]`
   arg on this connection maps to the connection. `ctx.notify(uuid,
   payload)` emits a `notify` frame if the route exists, else drops
   silently (mirrors the workstation inbox's silent-drop-when-closed,
   `callback_impl.py` `notify`).
2. **Transient services**: a ws service impl can return (or pass in
   a result structure) a *transient service object*; the library
   assigns a UUID, stores it in the table, and encodes it as
   `["service", {kind: "transient", id}]`. Canonical use:
   `subscribe(handle)` returns the unsubscribe handle as a transient
   service. Invokes addressed to a transient id dispatch to the
   stored object.

The table dies with the connection — no GC protocol needed.

### Transient stubs, client side

A transient descriptor decoded from connection (url U, generation G)
mints a stub bound to (U, G):

- Invokes on it route over pool[U] iff the live generation is still
  G; otherwise the invoke fills as `invoke_error` ("transient
  service broken") without sending anything.
- Escaping it as an *arg* is valid only for invokes routed over
  (U, G); any other destination (http service, other ws url, newer
  generation) is a runtime error at encode time. Encode happens at
  allocate time (`session.ts:1086-1189` does arg encoding up-front)
  but the generation can die between allocate and send, so we **check
  at allocate (fail fast) *and* at send (errored fill)** (resolved
  Q-L) — both cheap.
- Persisted transient stubs rehydrate **born-broken** (their
  generation no longer exists by definition).
- Workstation analogue throughout: a service registered by an RPC
  peer that crashed — calls error, nothing retries.

### Disconnect semantics (governing principle applied)

On a dropped ws connection: in-flight invokes fill as `invoke_error`
events; queued-not-yet-sent invokes likewise error (no silent replay
onto the next generation); transient stubs break; inbox routes die
(notifies stop arriving). For inboxes that hold a live subscription
over the dropped connection, the connector flips the inbox's
`connectionState` to `broken` (design/05 §Inboxes — the local
synthesis of what the workstation delivers as a
`CallbackSubscriptionEvent`, now that the precursor in part6
stage4post1 has landed). That is the uniform breakage signal the Part
1 dispatcher reload reacts to. Mutations are never rejected by any of
this; the session stays healthy.
