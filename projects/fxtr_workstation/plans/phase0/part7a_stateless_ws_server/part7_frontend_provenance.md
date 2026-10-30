# Part 7 — provenance through the HTTP frontend

> Status: IMPLEMENTED (2026-06-27) — all chunks landed + tested (escape decode,
> shortId `@N` port, connector core, demo + integration gate). The wire
> half (part 6) has landed; this is the **client** half — the HTTP connector
> consumes part 6's layered ids and surfaces provenance exactly as the
> workstation does. Build-side = `../part7b_publishing_flow/part5_layer_provenance.md`;
> wire = `part6_http_provenance.md`; full real-build demo = `../part7c_build_and_serve_demo/`.
>
> **Plan-local shorthand** (carried over from part 6): **E1** = the layered entity
> escape `["entity", data, prov]`; **P2** = the layered pin/discovery value
> `[data, prov]`. These labels are plan-only — kept out of code, comments, and the
> wire-protocol design doc; don't propagate them.

## Goal

A layered entity served over HTTP (a `(data, prov)` id) behaves on the client
**identically to a workstation-served one**: its `provenance_parent` is revealed
on metadata request, ref-links work, and — the load-bearing invariant —
**loading a layered entity yields layered children** (a report's figure refs come
back carrying their own provenance), so provenance propagates through every
subsequent load. The 7a governing principle applies: the connector *emulates a
workstation in the browser*, so it matches the workstation's representation
rather than inventing a parallel one.

## The core mechanism (verified against the Python scope)

The whole part hinges on how a *layered* id is loaded. The Python `FxtrScope`
already does exactly what the connector must mirror — `_load_arena_cbor`
(`v2/fxtr2/data/entity_ref_scope.py:3630-3666`):

> For a bare id, load the d-node's content (bare children). For a **layered** id
> `(data, prov)`, **load the *provenance form*** — the `prov` node (an `n`-node
> whose CBOR is the full entity content but with **provenance-level child refs**)
> — then walk the cbor and **rewrap each child**: `child_data =
> unwrap_provenance(child_prov)`, child ref → `(child_data, child_prov)`.

Two facts make this cheap (both verified):
- **An `n`-node's CBOR is the full content**, just with each child ref replaced
  by that child's prov node. So loading the prov form yields displayable content
  *and* the provenance-level children in one shot — no separate data-form fetch,
  no structural zip.
- **The `/data/{prov}` precomp already carries the children's `unwraps_to`.** The
  `/data` endpoint computes `compute_precomp_closure` over `[deps + node]`, and an
  `n`-node's deps *are* its child prov nodes — so the response bundles each
  `child_prov → child_data` edge. The connector reads `unwrap_provenance` straight
  off the precomp it just fetched, exactly as the Python arena reads it sync.

Loading recurses naturally: each layered child, when later loaded, fetches *its*
prov form and rewraps *its* children — one level per load, same as Python.

**Connector mirror — the one decode site.** Both the dispatch load and the
session's `allocateLoad` funnel through `connector.loadEntityData`
(`connector.ts:239-243` → `fetcher.load(idString)` → `cborToResultValue`). The
layered path lives here:
- resolve the stub's shortId → `(data, prov)` via the shortId table;
- **bare** → today: `fetcher.load(data)`, decode with a bare `entityRefForIdString`;
- **layered** → `fetcher.load(prov)` (fetches the `n`-node cbor + the child-unwrap
  precomp), then `cborToResultValue` with a **rewrapping** ref callback: given a
  child's prov idString, read `precomp[child_prov].unwraps_to = child_data` and
  intern the *layered* `(child_data, child_prov)` shortId. **Every** child in a
  prov form is itself a prov node (the store's prov path u-wraps even originally
  bare children — 7b.5), so every child unwraps; a missing `unwraps_to` is an
  **error**, not a silent bare fallback (matching the Python `_rewrap` assert —
  the `/data/{prov}` precomp always bundles every child's unwrap, so absence means
  a server/precomp bug, which must surface, not degrade to a bare child).

This is the only place the "children load layered" invariant is enforced, and it
is enforced for *every* layered load — there is no code path that produces a
layered entity whose children come back bare.

## Design by area

### `@fxtr-ui/core/src/escape.ts` — E1 decode

`decodeTagged`'s entity branch (`escape.ts:417-424`) is strict 2-element today.
Extend it to accept the E1 3-element `["entity", data, prov]` and **combine** the
lookup into one callback (decision 2026-06-27 — `lookupEntity` is an internal
opts callback, cheap to widen, and one callback reads better than two):

```
lookupEntity(idString: string, provIdString?: string): EntityRefStub
```
- 2-element → `lookupEntity(value[1])` (unchanged). The **workstation** connector
  stays 2-element — its `@N` provenance is *inside* the short_id string
  (`["entity", "d123@1"]`), so its `lookupEntity` is untouched.
- 3-element → `lookupEntity(value[1], value[2])`. Only the **HTTP** connector
  sends this; its `lookupEntity` interns a layered shortId from `(data, prov)`.
- Strict arity: exactly 2 or 3 (mirrors part 6's Python codec); 4+ or non-string
  elements throw.

`metadata.ts` already carries every provenance field — **no change** there.

### `shortid_table.ts` — faithful `short_ids.py` port (provenance-aware)

Today the table keys entities by data `idString` only, so `(data,None)` and
`(data,prov)` would **collide** to one shortId — wrong. Port the workstation's
`short_ids.py` allocation (decision 2026-06-27: faithful port, for consistency +
the eventual cross-backend parity test):

- Key entities by a **`ProvenancedKey(data, prov)`** (both halves bare local
  ids), not a bare idString.
- Allocate the `@N` family exactly as `short_ids.py:_allocate_entity_short_id`:
  - prefixable (`d`/`c`/`n`): first `(data, *)` claims the bare prefix
    (implicit `@1`); a late `(data, None)` → `{prefix}@0`; the self-`u` form
    `(X, u(X))` → `{prefix}@u`; each new distinct prov → `{prefix}@{N++}`.
  - non-prefixable (`.`/`!`/`p`/`u`): full id; `(data,None)` bare; `(X,u(X))` →
    `{prefix}@u`; `(data, prov)` → `{prefix}@{recursive shortId of (prov,None)}`.
- Reverse map `shortId → ProvenancedKey`, plus a `to_layered_fxtr_id`-style
  reconstruction. `classifyShortId` and all lookups stay **table-answered**
  (never parse the shortId) — unaffected by the new shape.
- **Persistence**: the serialized entry carries both halves
  (`{kind:"entity", idString, provIdString?}`); on rehydrate the per-data-prefix
  `@N` bucket state is **rebuilt from the entries** (scan + reconstruct the
  next-suffix high-water), so post-reload allocations keep ascending without
  colliding — the same property the service `s{N}` counter already persists.

### `connector.ts` `getEntityMetadata` — full workstation parity

Un-stub `getEntityMetadata` (`connector.ts:264-289`) to reproduce
`state.py:_compute_entity_metadata` field-for-field, now that the shortId resolves
to `(data, prov)`:
- `unprovenanced` = intern `(data, None)`; always present.
- `provenance_parent` = intern `(prov, None)` + `provenance_parent_id_string =
  prov.id_string()` — present iff the entity is layered.
- `unwraps_to` / `context_data` / `backstep` — when the **data half's tag** is
  `c`/`n`/`p`/`u`, layered exactly as the workstation does (e.g. `unwraps_to` is
  the layered `(unwrapped, data)` form), derived locally from the `/data` precomp
  + the data id (the connector has both). These fire only when inspecting a prov
  node directly; the common report case (a `d`-node) just gets `provenance_parent`.

(Decision 2026-06-27: full parity — `unwraps_to`/`context_data`/`backstep` layered,
not the previous bare "accepted divergence".)

### P2 pin decode — `listEntities` / pin loads / discovery

Wherever the connector ingests a pin/discovery id (the `/pins`, `/pin`, and
`/discovery` P2 values), decode the part-6 shape: a bare string interns
`(data, None)`; a `[data, prov]` array interns the layered `(data, prov)`. Strict
shape (string or 2-array), reusing the same decode discipline as part 6's Python
`_decode_pin_value`.

### Persistence / rehydration

Persisted session state stays shortId-mode (`persistence.ts` `userState`,
unchanged shape). Because the shortId table now carries the prov half and
rehydrates it, a persisted layered ref round-trips with its provenance intact —
no change to the session blob format, only to what the table entry holds.

## The invariant (stated explicitly, since it gates the design)

**Children of a layered entity always load layered.** Enforced at the single
`loadEntityData` rewrap site: a layered load fetches the prov form and rewraps
*every* child to `(unwrap(child_prov), child_prov)`. There is no path that yields
a layered entity with bare children. This is why the connector work lands as one
coherent unit (below) rather than "shallow first" — landing layered pins/escape
*without* the load rewrap would let a layered entity load with bare children,
silently violating the invariant.

## Chunking (each chunk self-contained; no chunk leaves a broken invariant)

1. **`@fxtr-ui/core` escape E1 decode.** The 3-element entity decode + combined
   `lookupEntity(data, prov?)`. Pure codec; no connector consumes it yet, so it
   changes no behavior on its own. Unit-tested via escape round-trip. *(Optional
   to fold into chunk 3 if we'd rather not land an unused decode path; kept
   separate because it's the shared-core seam.)*
2. **shortId table port.** Make `shortid_table.ts` provenance-capable (the
   `short_ids.py` `@N` port, `ProvenancedKey`-keyed, persistence rebuild) **with
   unit tests**, but nothing yet interns a layered key — so the table behaves
   exactly as today for the existing bare callers. Self-contained infrastructure
   (like 7b.5's "primitive first" commit); no invariant impact because no layered
   ids exist yet.
3. **Connector layered handling (the core unit).** Wire it all together
   atomically: P2 pin decode + escape `lookupEntity` layered intern + the
   `loadEntityData` prov-form rewrap + `getEntityMetadata` full parity +
   persistence rehydrate. After this chunk a layered entity is *fully* handled —
   pins layered, **loaded children layered**, metadata shows provenance, survives
   reload. This is the minimal complete unit that upholds the invariant.
4. **Demo + acceptance gate** (next section).

*(Doc updates — connector-http CODE_OVERVIEW + the 7a design docs — ride along in
the chunk that touches each area, per repo convention.)*

## Acceptance gate — demo provenance routes + integration test

Mirrors how part 5 validated 7a.1–4, as the **final acceptance gate** for the
provenance arc's serve+client side (7c remains the full real-build pipeline).

- **Demo backend** (`examples/fxtr_http_demo/server/demo_backend.py`): mint a
  genuinely-provenanced entity using **existing demo types** (e.g. a provenanced
  `Note` referencing a `Photo`) via a c-node + a `provenance_context` scope
  (the `debug_ui_sessions_demo.py` / part-6 test pattern), pin it, and pass
  `provenanced=True` to `create_fxtr_app` + `ServiceRegistry`. Keep the existing
  bare pins too, so the demo shows both side by side.
- **Visual**: the existing roots panel lists it; clicking through to metadata
  shows `provenance_parent` (and, for a loaded parent, the child ref carries
  provenance). Optionally a tiny "provenance" affordance, but the existing
  metadata panel already renders it.
- **Automated integration test** (the real gate): a `@fxtr-ui/connector-http`
  vitest that spawns the Python demo backend over the wire (the
  `_demo_*.py` + `_integration_harness.ts` spawn-`READY`-drive pattern from the
  connector-workstation tests) and asserts, against the real connector:
  - a layered pin's `getEntityMetadata` returns `provenance_parent` populated;
  - **loading the layered parent yields a child whose ref is layered** (the
    invariant — nested provenance), asserted via the child's `getEntityMetadata`.

## Out of scope / boundaries

- **7c** (full nix build → serve → client E2E) is separate; part 7 mints the
  provenanced entity directly (no build).
- **`@fxtr-ui/reports` / dispatch / the metadata panels** are unchanged — they
  consume the existing `EntityMetadata`; only the connector populates it.
- The **workstation** connector + wire are untouched (its provenance already
  works; the `escape.ts` change is backward-compatible — it stays 2-element).

## Rough steps

- [x] `@fxtr-ui/core/escape.ts`: E1 3-element entity decode + combined
      `lookupEntity(data, prov?)`; escape unit tests. (Tests live in
      `connector-workstation/tests/escape.test.ts`, the codec's existing test
      home; the bare workstation path is untouched + the prior strict-2-arity
      error test updated since 3-element is now the valid layered form.)
- [x] `shortid_table.ts`: faithful `short_ids.py` `@N` port, `ProvenancedKey`-keyed
      buckets (`@0`/`@N`/`@u` + non-prefixable recursion), `entityKeyFor` reverse
      lookup, persistence rebuilt from entries (buckets + `@N` high-water + prefix
      claims); unit tests. Connector still calls the single-arg form → behaves as
      today (no layered keys interned until chunk 3).
- [x] connector-http core: P2 entity decode (the TS connector reads `/discovery`,
      not `/pins`/`/pin` — those are Python-client paths) + layered `lookupEntity`
      intern (`internEntity(data, prov?)`, wired into the session escape decode) +
      `loadEntityData` prov-form rewrap (children load layered; missing child
      unwrap = error) + `getEntityMetadata` full parity (`provenance_parent` from
      the table; layered `unwraps_to`, bare `context_data`/`backstep`). Persisted
      state is shortId-mode so it rehydrates unchanged (the table carries the prov
      half). `provenance.test.ts` covers all of it with a mocked backend.
- [x] Demo backend provenance pin (`provenanced=True`, existing types) +
      connector-http spawn-over-wire integration test (metadata + nested). The
      kitchen-sink `_demo_backend.py` serves a genuinely-provenanced
      `Parent`→`Child` (`prov_root`); `integration.test.ts` asserts
      `provenance_parent` on it and that its loaded child comes back layered (the
      invariant) over the real wire. The `examples/fxtr_http_demo` backend serves
      a provenanced `Note` (`note-prov`) that *links* to a provenanced `Photo`
      (the `Note` type carries a `links` dict, like `simple_tasks`'
      `NoteWithLinks`) — so the demo exercises nested-object provenance too; the
      note FullPanel renderer renders the links as `RefLink`s for navigation.
      Smoke-tested: the backend compiles + serves `note-prov` layered with the
      linked photo's own prov node in its precomp.
- [x] Update the 7a design docs (`design/01` wire shapes; `design/03` metadata
      derivation from the table key, not the `@`-form; `design/04` the layered
      shortId allocation) + this plan's checkboxes. (`@fxtr-ui/connector-http` has
      no CODE_OVERVIEW; the source docstrings carry that role. `design/05` needed
      no provenance change.)
