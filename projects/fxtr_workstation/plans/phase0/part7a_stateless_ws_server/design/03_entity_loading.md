# 03 — Entity loading: /data, client-side decode, metadata

Status: resolved. The piece that makes a renderer unable to tell
which connector loaded its entity.

## Re-orientation pointers

| What | Where |
|---|---|
| `/data` endpoint (py) | `v2/fxtr2/experimental/http/asgi_app.py:74-…` (returns CBOR `{bytes, precomp}`) |
| `/data` endpoint (ts) | `v2/ts/packages/fxtr-http-server/src/server.ts:175-214`; precomp closure `:29-79` |
| Precomp entry shape | `v2/fxtr2/data/arena_util.py:215-313` (`PrecompEntry`: `unwraps_to` / `ctx_node` / `backstep`; `compute_precomp_closure`) |
| TS CBOR decode | `v2/ts/packages/fxtr/src/core/entityCore.ts:452-454` (`fxtrCborLoads`), tag decoders `:424-438` (33000/33001/33002), `RawWeakFxtrID`/`RawStrongFxtrID`/`RawFxtrEntityMarkerID` `:351-391` |
| TS HTTP arena client | `v2/ts/packages/fxtr-http-client/src/client.ts:135-187` (`HttpReadableArena.load` → `FxtrCbor`, `loadFlat` → flat deps) |
| What the workstation sends for a load (the parity target) | `state.py:3379-3452` (`load_worker`: `load_scope.load_cbor` `:3440`), `:3176-3243` (`_phase3_commit_load_result`: `escape(cbor, …)` → `LoadResultEvent.payload`), `_walk_fxtr_ids :2958-3024` |
| `load_cbor` itself | `v2/fxtr2/data/entity_ref_scope.py:3938-3956` (loads with `UnknownMutableEntity` codec, returns raw CBOR tree with `Raw*` wrappers at ref leaves) |
| Frontend decode of that payload | `fxtr-ui-connector-workstation/src/escape.ts:324-364` (`unescapeWire`, result mode) |

## The decode, precisely

The workstation pipeline for `allocateLoad` is:

```
load_cbor(id)            # CBOR tree, Raw*FxtrID wrappers at ref leaves
  └─ escape(...)         # Raw* → ["entity", shortId]; bytes → b64; lists wrapped
       └─ (wire) ──────► unescapeWire(...)   # tags → interned EntityRefStubs
```

The HTTP connector replaces the middle with a local transformation:

```
GET /data/{id} → {bytes, precomp}
  └─ fxtrCborLoads(bytes)        # same CBOR tree shape as load_cbor's
       └─ cborToResultValue(...) # Raw* → interned EntityRefStub; Uint8Array kept;
                                 # dicts/lists recurse; primitives pass through
```

`cborToResultValue` is the new piece (lives in
`@fxtr-ui/connector-http`): a single walk replacing
`RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID` leaves with
`connector.entityRef(shortIdFor(innerId.idString()))` — allocating
shortIds through the connector table as a side effect — and leaving
everything else alone. Note there is **no JSON round trip**: real
lists stay lists (no `[[...]]` wrapping needed), bytes stay
`Uint8Array` (no base64). `$fxtr.ctx` / `$fxtr.type` fields survive
as ordinary dict entries whose values become stubs — which is
exactly how the dynamic dispatcher finds an entity's type today.

**Parity contract (a literal test)**: for the same entity served by a
workstation and by the Python stateless backend,
`unescapeWire(workstation LoadResultEvent.payload)` and
`cborToResultValue(fxtrCborLoads(/data bytes))` are structurally
equal, with stub equality = shortId-table-mediated identity.

## Reuse of `HttpReadableArena` vs direct fetch

`HttpReadableArena` (`fxtr-http-client/src/client.ts:76-301`)
already does fetch + decode + precomp ingestion + child-ID tracking.
Provisional decision: the connector **uses it** (one instance per
connector) rather than re-implementing fetch/decode — we get precomp
bookkeeping (`_precompUnwrapsTo` etc.) and an in-memory cache of
loaded nodes for free, and `loadEntityData` / `allocateLoad` /
metadata all read through it. Implementation-time call (Part 3): it
implements `ReadableArena` for scope integration we don't need; if the
unused surface drags weight, extract the fetch+decode core instead.
Not a design fork — decide when writing the code.

A pleasant upgrade over the workstation's out-of-band path: children
in `/data` payloads are **fully loadable** (the server is stateless;
any reachable id works), unlike workstation direct-load descriptors
(`get_or_assign_entity_inactive` children, which aren't further
loadable). `connector.loadEntityData` therefore has no
"descriptor-only children" caveat here.

## Metadata ops, client-side

`allocateGetEntityMetadata` resolves locally — same `EntityMetadata`
shape (`fxtr-ui-core/src/metadata.ts`) the workstation emits
(reference: the `GetEntityMetadataOp` payload description in
`projects/fxtr_workstation/CODE_OVERVIEW.md`):

  (The stub's table entry is its `(data, prov)` key — the prov half
  comes from the wire, *not* from parsing any `@` form.)

- `id_string`: the data-half local id_string of the stub's key.
- `provenance_parent` / `provenance_parent_id_string`: the key's prov
  half (present iff the entity is layered); the parent ref is interned
  as the bare `(prov, null)` key.
- `unwraps_to`, `context_data` (c-tag), `backstep` (p-tag): only when
  the data half's *tag* is a provenance node (`c`/`n`/`p`/`u`), from the
  precomp closure (fetch `/data/{data}` first if this id hasn't been
  loaded — every `/data` response carries the precomp closure for its
  seeds). `unwraps_to` is the **layered** `(unwrapped, data)` form;
  `context_data` (precomp `ctx_node`) / `backstep` are bare.
- `unprovenanced`: the bare `(data, null)` key, always present.

`allocateGetServiceMetadata`: resolves **locally**, mirroring the
workstation (resolved Q-H). The server bundles `service_type_id` (the
type entity's `id_string`) on every descriptor it introduces
(discovery + invoke results, design/01), so the connector reads
`type_id_string` straight off the stub's descriptor and fills the
same `ServiceMetadata` shape the workstation emits — **no invoke, no
"unknown" sentinel**. (This is the string id; it is distinct from the
*loadable* type entity the dynamic dispatcher fetches via the
`$fxtr.type` invoke.) A descriptor that somehow arrived without the
annotation errors the metadata slot rather than fabricating a value —
matching the workstation's "record disappeared" error style; in
practice every server-introduced descriptor carries it.

## Caching

`/data/{id}` responses get `Cache-Control: public, immutable`
(content-addressed). The connector additionally holds the
arena-level in-memory cache above; persisted entity-payload caching
is explicitly **not** done (overview.md persistence rule).
