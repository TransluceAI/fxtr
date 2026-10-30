# Part 6 — expose provenance over the HTTP endpoints

> Status: SCOPED + code-checked + wire shape DECIDED + open questions resolved
> (2026-06-27), not yet implemented. Added when the publishing flow was replanned
> around *provenance-through-layers*. This is the **serve-side wire** half of the
> provenance arc: let the HTTP surface carry an entity's provenance, not just its
> data id. Build-side half = `../part7b_publishing_flow/part5_layer_provenance.md`
> (provenanced layers); client half = `part7_frontend_provenance.md`; combined
> demo = `../part7c_build_and_serve_demo/`.
>
> **Decision (2026-06-20): an EXPLICIT layered representation on the wire (P2 +
> E1), NOT the `id_string` `@`-form.** A layered `FxtrID` *does* have a
> round-tripping single-string form (`data@prov` via
> `id_string(include_provenance=True)` / `from_id_string`), and an early reading
> noted that would be "free." We rejected it: `include_provenance` is effectively
> debug-only today, and overloading the single-id escape/pin slot with a secretly
> two-id string makes an internal serialization detail load-bearing in the
> protocol and is a footgun for any consumer that treats the slot as one opaque id
> (e.g. `GET /data/{that}`). Instead the wire carries the prov id **explicitly,
> alongside** the data id, keeping "the id slot is always a bare data id"
> invariant.

## Motivation

A layered fxtr id is `(data, prov)` — a data id plus a provenance-node parent
(`FxtrID._raw_data_id` + `FxtrID._provenance_parent`,
`v2/fxtr2/data/entity_core.py:289-294`). The HTTP surface currently emits **bare
data ids** everywhere, so even once a provenanced layer (7b part 5) yields
layered ids, the provenance can't reach the client.

## Current state (code orientation)

- **`id_string` is bare by default; the `@`-form is debug-only and we won't use
  it on the wire.** `FxtrID.id_string(*, include_provenance=False)`
  (`entity_core.py:443-447`) returns the bare *local* id; `include_provenance=True`
  appends `@<prov>` and `from_id_string` parses it back (`:368-372`). It round-trips,
  but it's an internal repr (debug prints) — see the decision above for why the
  protocol uses an explicit shape instead.
- **Every emit point currently calls the bare `.id_string()`:**
  - `GET /pins` → `{name: fid.id_string()}` JSON (`asgi_app.py:136`).
  - `GET /pin/{name}` → CBOR `{"id": fxtr_id.id_string(), "precomp": …}` (`asgi_app.py:126`).
  - service results → `["entity", value.id_string()]` (the list-escape codec,
    `service_codec.py:189-217`, `TAG_ENTITY`; for `FxtrID` / `EntityRef` / `Raw*FxtrID`).
  - `/discovery` entities → `{name: fid.id_string()}` (`service_registry.py:281`).
  - `GET /data/{id_string}` is fetched **by local id** and returns
    `{"bytes", "precomp"}` (`asgi_app.py:74-104`); the client's `_fetch_data` uses
    the bare local id for the path (`client.py:108`).
- **Decode points (Python client + codec) use `from_id_string`** (`client.py:386`
  /pin, `:423` /pins, `service_codec.py:332-335` unescape). These become
  structured-aware (below) — the Python client is un-stubbed in this part too, not
  deferred (decision 2026-06-27).
- **Precomp only rides with `/data` and `/pin` — not `/pins` or service results.**
  `/data` → `{bytes, precomp}`, `/pin` → `{id, precomp}` (single pin); `/pins`
  returns ids only and **service results carry no precomp at all** (the codec
  escapes a bare `["entity", id]`, no closure — `service_codec.py:189-217`, zero
  precomp handling in `service_*.py`). The only way to get precomp for a `/pins`
  entry or a service-returned ref is to `GET /data/{id}` (the client lazy-loads;
  precomp ingested at `client.py:85-104`).
- **`/data` precomp is *downward only*.** `compute_precomp_closure` records
  `unwraps_to` / `ctx_node` / `backstep` (`arena_util.py:257+`) as bare
  id_strings — prov→data, never data→prov. So a **bare data id can't reveal its
  own provenance**; the pin/ref itself must carry the prov id. This is *why*
  provenance has to ride the wire explicitly and can't be reconstructed from
  `/data`.

## The wire shape (decided)

The data id stays a first-class bare element everywhere; the prov id is added
explicitly alongside, self-describing (a layered value is structurally distinct
from a bare one). The server reads the prov id straight off the `FxtrID`s it is
already handed (`fid._provenance_parent`) — **no new server input plumbing** (see
"Source of the prov id" below).

> **Plan-local shorthand.** This plan refers to the two shapes below as **E1**
> (the layered entity escape) and **P2** (the layered pin/discovery value) —
> leftover labels from the options weighed during design. They are **deliberately
> kept out of code, comments, and the wire-protocol design doc**
> ([`design/01_wire_protocol.md`](design/01_wire_protocol.md) describes the shapes
> directly); don't propagate them.

- **Escaped entity ref — E1 (extend the tuple).** Bare `["entity", "<data_id>"]`
  (unchanged); layered `["entity", "<data_id>", "<prov_id>"]`. Both are bare local
  id_strings. A consumer reading only element `[1]` (fetch `/data`, key a table)
  is unaffected; a provenance-aware consumer reads `[2]`. The decoder branches on
  arity. Only `FxtrID` / `EntityRef` can be layered; `Raw*FxtrID` come from CBOR,
  which never carries provenance, so they stay bare (2-element) always.
- **Pin value — P2 (tuple value).** `/pins`: bare `{name: "<data_id>"}`
  (unchanged); layered `{name: ["<data_id>", "<prov_id>"]}`. Decode: string ⇒
  bare, array ⇒ layered.
- **`/pin/{name}`** mirrors P2 for its `id` field (bare string vs `[data, prov]`),
  **and — the one precomp change — when the pin is layered its `precomp` is the
  closure of the PROV node** (run the existing `compute_precomp_closure` over the
  prov node id instead of the data id). The prov node's closure *already contains
  the data id* (via its `unwraps_to` edge), which then expands to everything the
  bare path produced — so this is a strict superset, not an extra merge. **The
  seed is always the prov node for a layered pin — never the data node.** `/pin` is
  the only endpoint that *both* bundles precomp *and* now carries provenance, so
  the prov node's edges must be in the bundle — otherwise the client (which gets
  precomp with `/pin`) would need an extra `GET /data/{prov_id}` to unwrap the prov
  node back to its data. (`/pins` + service results carry no precomp, so they need
  no precomp change — the client `/data`-fetches the prov node on demand, exactly
  as it does any data id today.)
  - **Unfetchable seed → error, never a partial/degraded response.** When
    `precomp` is present it is the *complete* closure of the emitted id (the prov
    node for a layered pin, the data node for a bare one); we never emit a partial
    or wrong-seed closure. If the seed isn't deep-fetchable
    (`scope._deep_fetch(seed) is None`) the server returns a **`500`** — a pin name
    that resolves but whose entity the scope can't serve is a server inconsistency
    / fetch failure, not a success (distinct from the existing `404` for a pin
    *name* not in `pins`). `precomp` is omitted **only** when a *successful* fetch
    yields a genuinely empty closure (an edge-less entity, e.g. a plain `d` node);
    that is the one legitimate absent-precomp case. We do **not** fall back to
    seeding the data id for a layered pin (that would yield a closure missing the
    prov→data `unwraps_to` edge — a corrupt, mismatched bundle).
  - **This also fixes a pre-existing bug in the bare `/pin` path**, which today
    returns a `200` with no precomp when `_deep_fetch is None` (`asgi_app.py:116-128`),
    silently masking a missing/unreachable pinned entity. `/data` already errors
    correctly on `_deep_fetch is None` (`404`, `asgi_app.py:84-86`); `/pin` is made
    consistent (`500`, since the pin name itself resolved). Part 6 corrects this
    while rewriting the handler so bare and layered share one "fetch-or-fail" rule.
- **`/data/{id}` is unchanged** — by-id and bare. A prov node is just another
  entity fetched by its own local id; its `/data` yields its own `unwraps_to`
  precomp. (Consistent with 7b part 5's by-id/by-pin split.)

So: **provenance *ids* ride inline (P2/E1); provenance-*node* bytes/precomp are
fetched on demand via the existing `/data`** — except `/pin`, which bundles the
prov node's precomp because it already bundles precomp.

## The `provenanced` gate (server-side opt-in)

Provenance is exposed only when the serving author opts in, so a server can't
leak provenance the author didn't intend to publish. A single boolean controls
**all** emission points:

- `create_fxtr_app(scope, pins, *, provenanced: bool = False)` — gates `/pins`
  (P2) and `/pin` (P2 `id` + prov-node precomp).
- `ServiceRegistry(scope, *, base_url=None, provenanced: bool = False)` — gates
  `/discovery` entity entries (P2) and is threaded into the service endpoints so
  service-result escapes (E1) are gated too.
- `escape_value(value, *, register_transient=None, provenanced: bool = False)` —
  emits E1 only when `provenanced`; otherwise bare (today's behavior). Threaded in
  by `rest_service_route`/`make_rest_handler` and `ws_services_endpoint`
  (result + notify escapes).
- `server.py` helpers (`serve_until` / `serve_forever` / `serving_context`) thread
  `provenanced` through to `create_fxtr_app`.

**Default `False` everywhere** ⇒ today's exact bare behavior, so all existing
callers/tests are unchanged and provenance is strictly opt-in. The 7c serving
script passes `provenanced=True`. When `provenanced=False` the gate simply
*skips the layered branch* — nothing is mutated or stripped, the server just emits
the bare form it always has (even when the `FxtrID`s it holds happen to be
layered). **Decode is never gated** — `unescape_value` and the Python client
always accept the layered form; the gate is purely about emission.

## Source of the prov id (server-side, resolved)

No new argument to `create_fxtr_app` / `ServiceRegistry` is needed for the prov
id itself. The `pins` dict is already `dict[str, FxtrID]`, and a layered pin's
`FxtrID` already carries `_provenance_parent`; service impls likewise return
`FxtrID`s that may be layered. 7b part 5's reader / `FlakeManager.mount_pin`
produce layered `FxtrID`s, and 7c's serving script plumbs those into the `pins`
dict (and any service results). So the server detects layering with
`fid._provenance_parent is not None` and reads the prov id as
`fid._provenance_parent`; `fid.id_string()` (bare default) is the data half.

## Client decode (Python + TS)

Unlike the `@`-form, the explicit shapes need the decoder to branch on
arity/type. Both the **Python client** (this part) and the **TS connector** (part
7) un-stub:

- **Python `service_codec.unescape_value`** — a 3-element `["entity", data, prov]`
  decodes to a layered `FxtrID` (re-pair `data` + `prov` via `from_id_string` into
  one `FxtrID(raw_data_id=…, provenance_parent=…)`); 2-element stays bare. Ungated
  (decoding the layered form is always safe).
- **Python `client.py`** —
  - `list_pins` (`/pins`): a `[data, prov]` value → layered `FxtrID`; a string →
    bare (today).
  - `HttpFxtrReadNamespace._retrieve_impl` (`/pin`): decode `result["id"]` P2 →
    layered `FxtrID`; the bundled prov-node precomp is ingested by the existing
    `_ingest_precomp` (precomp keys stay bare node id_strings — no change to
    `_ingest_precomp` itself). Track both the data and prov nodes so the attach +
    `unwrap_provenance` resolve. (Settle the exact attach/track wiring during
    implementation — see "Details to settle".)
- **TS connector (part 7, not here)** — `@fxtr-ui/core/escape.ts` 3-element
  unescape + the `[data, prov]` pin decode + carrying the prov half in the shortId
  table. Flagged here only so the two parts stay in sync on the exact shape.

## Nested refs inside a loaded entity — OUT of scope for part 6 (resolved)

Nested refs (e.g. a report's figure refs) live in the parent's CBOR as **bare
data ids** (provenance is never in CBOR), so E1 on the *escape* can't reach them.
Their provenance is conveyed by serving the entity's **prov form** (the `n`-node,
whose child refs are themselves prov nodes — 7b part 5's finding) and fetching
those prov nodes via `/data` by id — which the server **already does** (any node
is fetchable by its own local id). So part 6 needs **no nested-specific
server work**: once the layered pin/ref delivers the prov id (the `n`-node), the
client loads the prov form and walks its prov-node children itself. Consuming that
to surface nested provenance is **part 7** (the connector loading the prov form +
answering metadata) and **7c** (the end-to-end demo). The earlier note flagging
this as an unsettled server-side sub-problem was an under-informed draft; it is
settled as "nothing to do server-side."

## Resolved decisions (summary)

- **Wire shape:** explicit P2 (pins) + E1 (escape), not the `@`-form (top of doc).
- **`/pin` precomp:** closure of the **prov node** (`compute_precomp_closure`
  over `[prov_id]`), a superset of the data closure since the prov node's
  `unwraps_to` pulls in the data id. No special-casing, no merge. Seed is always
  the prov node, never the data node. **Unfetchable seed → `500`** (this also
  fixes the pre-existing bare-`/pin` bug of returning `200`/no-precomp on
  `_deep_fetch is None`); `precomp` is omitted **only** for a successful but
  genuinely empty (edge-less) closure — never a data-seeded / mismatched partial.
- **`provenanced` gate:** one bool, default `False`, gating every emit point
  (pins / pin / discovery / service-result escapes); decode always accepts
  layered.
- **Source of prov id:** `fid._provenance_parent` on the `FxtrID`s already passed
  in; no new server argument.
- **Python client + codec un-stubbed in part 6** (not deferred): `unescape_value`,
  `list_pins`, `_retrieve_impl`.
- **Nested refs:** out of scope server-side; handled in part 7 / 7c via serving
  the prov form + by-id `/data`.
- **`@`-form on the wire — REJECT (corrected 2026-06-27).** Earlier this was
  "rejection not necessary, a stray `@` is rejected by normal parsing" — that was
  **wrong**: `FxtrID.from_id_string` *silently parses* an `@`-form into a layered
  id (`entity_core.py:368-372`), so `GET /data/d123@n456` would silently return the
  `d123` bytes (ignoring `@n456`), and an `@` inside an E1 element would silently
  nest a second provenance level. Since the invariant is "every id on the wire is a
  bare local id," that must be **enforced**, not assumed — see the fail-loud
  invariants below.

## Fail-loud invariants (no silent tolerance)

The `/pin` fetch-or-fail fix generalizes: every malformed / missing / unexpected
input must fail, never silently coerce, drop, or partially emit. The
implementation must hold all of these (each gets a test):

- **Strict arity on decode.** An `["entity", …]` tag is valid only at length 2
  (bare) or 3 (layered); a pin/discovery value only as a string (bare) or a
  2-element array (layered). Any other shape → error. Extend the existing strict
  `len != 2` guard in `_decode_tagged` to "2 or 3," do **not** loosen it to
  `len >= 2`; never read `[1]`/`[2]` while ignoring extra elements.
- **Bare-id elements reject `@`.** Every id_string ingested from the wire — the
  `/data` + `/preview` path params, the data/prov elements of E1, the two halves
  of a P2 value — must be a bare local id. Parse with
  `FxtrID.from_id_string(..., allow_provenance=False)` (a core-level guard that
  rejects the `@`-form), since the default `from_id_string` would otherwise
  silently parse the `@`-form into a layered id.
- **`Raw*FxtrID` provenance assert.** The escape path emits `Raw*FxtrID` bare on
  the premise that CBOR never carries provenance; `assert inner_id._provenance_parent
  is None` rather than silently dropping it if that premise is ever violated.
- **No degraded layered return (client).** The client never returns a layered
  `FxtrID` whose prov node it can't resolve. If the prov node wasn't tracked /
  its precomp wasn't ingested (so `unwrap_provenance` would answer `None`), that's
  an error — not a layered id that silently loses its provenance downstream.

## Details settled during implementation

- **Python `_retrieve_impl` attach/track for a layered id — RESOLVED.** It mirrors
  the layered-store reader exactly: seed/attach on the bare prov node (its
  bundled `unwraps_to` precomp pulls the data node in), then
  `scope.embed_as_provenance_parent(seed)` reconstructs the validated layered id —
  which raises if the prov node can't be tracked/unwrapped (resolve-or-fail, never
  a silently-`None` layered id). Confirmed by the `test_client.py` round-trip,
  including nested-child provenance after a load.

## Implementation steps

- [x] `design/01_wire_protocol.md`: document the E1 layered entity escape
      (3-element, arity-branch) + the P2 layered pin value (`[data, prov]`) in the
      escape-family + identifier spec, and note the `provenanced` server gate.
- [x] `service_codec.py`: `escape_value` gains `provenanced` and emits E1 for
      layered `FxtrID`/`EntityRef`; `unescape_value` decodes 3-element E1 → layered
      `FxtrID` (ungated). `Raw*FxtrID` stay bare **with an
      `inner_id._provenance_parent is None` assert** (no silent drop). Extend
      `_decode_tagged`'s entity arity guard to "2 or 3 only" (not `>= 2`); reject
      `@` in each id_string element (`from_id_string(allow_provenance=False)`). Unit tests in
      `test_service_codec.py`, including malformed-arity + `@`-bearing-element → error.
- [x] Service endpoints: thread `provenanced` through `rest_service_route` /
      `make_rest_handler` and `ws_services_endpoint` (result + notify) into
      `escape_value`.
- [x] `service_registry.py`: `ServiceRegistry(provenanced=…)`; `discovery()`
      entity entries emit P2 when layered + gated; pass `provenanced` into the
      endpoints built by `routes()`. Tests in `test_service_registry.py`.
- [x] `asgi_app.py`: `create_fxtr_app(provenanced=…)`; `/pins` emits P2, `/pin`
      emits P2 `id` + prov-node precomp (closure over `[prov_id]`), both gated +
      back-compat. Rewrite the `/pin` handler to **fetch-or-fail**: `_deep_fetch`
      of the seed (prov node if layered, data node if bare) returning `None` → `500`
      (fixing the pre-existing silent-`200` bug); `precomp` omitted only for a
      successful empty closure. Add the bare-id `@`-guard to the `/data` +
      `/preview` path-param parse (`from_id_string(allow_provenance=False)`). `server.py`
      helpers thread the flag through. Tests in `test_asgi_app.py`, including
      regressions that an unfetchable pin → `500` (not a bare `200`) and an
      `@`-bearing `/data` path → error. (P2 value encoding lives in the shared
      `service_codec.encode_pin_value`, used by both `asgi_app` + the registry.)
- [x] `client.py`: `list_pins` + `_retrieve_impl` decode P2 → layered `FxtrID`
      via the shared `_decode_pin_value` (strict shape: string or 2-array only,
      else error; halves parsed with `allow_provenance=False`). `_retrieve_impl`
      mirrors the layered-store reader: seed/attach on the bare prov node, then
      `scope.embed_as_provenance_parent` — resolve-or-fail, never a layered id
      whose provenance can't resolve. Tests in `test_client.py`.
- [x] Endpoint/unit round-trip: a layered entity round-trips `(data, prov)` over
      `/pins` + `/pin` (with prov-node precomp), decoding back to a layered
      `FxtrID` through the Python client; a bare entity is byte-unchanged;
      `provenanced=False` over a layered pin emits the bare form. **Nested
      provenance also round-trips for free**: loading a layered parent fetches its
      prov form (the `n`-node) by-id via `/data`, so `parent.child` comes back
      layered — confirming the part-6 serve side suffices for nested (the
      remaining nested work is purely part-7 frontend, as scoped). Shared
      provenance scaffolding (`make_c_node`) lives in `test_http/conftest.py`.
- [x] Update `v2/CODE_OVERVIEW.md` (the `experimental/http` service-layer entry:
      note the `provenanced` gate + P2/E1 wire forms).
- [ ] Client decode (part 7): structured E1/P2 handling in `@fxtr-ui/connector-http`.

## Suggested commit sequence

All `fxtr ws 7a.6 …`. Pause for review at each boundary.

1. **Codec E1 + gate** (`v2`). `escape_value(provenanced=…)` E1 emit +
   `unescape_value` E1 decode + endpoint threading; `test_service_codec.py` (and
   the rest/ws endpoint tests if they assert escapes). Self-contained codec change.
2. **Endpoints: pins / pin / discovery + `provenanced`** (`v2`). `create_fxtr_app`
   + `server.py` helpers + `ServiceRegistry` gate; `/pins` P2, `/pin` P2 +
   prov-node precomp, `/discovery` P2; `test_asgi_app.py` + `test_service_registry.py`.
3. **Python client decode** (`v2`). `list_pins` + `_retrieve_impl` P2 decode →
   layered `FxtrID`; `test_client.py` round-trip. After this the Python serve↔consume
   path round-trips provenance on its own.

`CODE_OVERVIEW.md` updates ride along in whichever commit touches that area.
