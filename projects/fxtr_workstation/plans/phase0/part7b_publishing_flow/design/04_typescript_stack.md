# Piece: the TypeScript stack (entity core, HTTP client, MDX transform)

> Status: updated after the first design sync (2026-06-12).
>
> **DECISION [OPEN-6/OPEN-4]: part 7b does not build on `v2/ts`.** The MDX
> step is `fxtr_report_builder` (§4 below) plumbed into nix — Python-driven,
> provenance handled Python-side, its bundled TS subprocess kept opaque. The
> TS-native layer reader/writer (§5) and the v2/ts redo (provenance +
> services + token scopes, then retire `fxtr_report_builder`) are deferred —
> see `../followups.md`. §§1–3 remain as orientation for that future work.

The publishing flow needs a TypeScript processing step that (a) compiles MDX
into entity payloads, (b) *loads* input entities (from git-namespace pins +
python-built layers), and (c) *exports* the results back as a layered entity
that nix can treat as a derivation output. This doc describes the TS pieces we
have today and how far each gets us.

## 1. `@fxtr/core` — the TS port of the fxtr2 data layer

Location: `v2/ts/packages/fxtr/` (overview: `v2/ts/CODE_OVERVIEW.md`).

An idiomatic TS port of the *fundamental* data layer: `FxtrID`, canonical CBOR
encoding with the fxtr tags (33000 marker / 33001 weak / 33002 strong), memory
arenas, scopes, entity refs, and the `bicodec` declarative entity-definition
system. Explicitly noted as "somewhat out of date relative to the latest fxtr2
design point, but still usable and worth keeping."

Key properties relevant to part 7b:

- **Content addressing matches Python.** `fxtrCborDumps` uses RFC 7049
  canonical key sorting, so a TS-stored entity gets the same `d…` id as the
  Python encoding of the same data. This is what makes "TS writes entities,
  Python/nix reads them" possible at all.
- **No provenance support.** `FxtrID._provenance_parent` is *not* ported
  (`v2/ts/CODE_OVERVIEW.md`, "Key Differences" table). Anything
  provenance-layered must either be handled as opaque ids (see the report
  builder's approach below) or stay on the Python side.
- **Codecs are explicit, not registry-based.** `EntityRef<T>` carries its load
  codec; `bicodec.entity(typeId, schema)` defines types. There is no global
  type registry, so the TS step must know the entity shapes it manipulates (or
  treat them as `UnknownEntity`).
- **Scopes + namespaces.** `FxtrScope` (`scope/scope.ts`), `FxtrReadNamespace`
  (`namespace/readNamespace.ts`) with `retrieve` / `tryRetrieve` / `listPins`,
  mirroring the Python `FxtrReadNamespace` ABC.

Where to look:
- `v2/ts/packages/fxtr/src/core/entityCore.ts` — FxtrID, CBOR tags, dumps/loads.
- `v2/ts/packages/fxtr/src/common/bicodec.ts` — entity definitions.
- `v2/ts/packages/fxtr/src/scope/scope.ts` — scope lifecycle (incl.
  `FXTR_SCOPE_INTERNALS` symbol gate used by the HTTP packages:
  `deepFetch`, `unsafeAttachFromExternalArena`, `mainArena`).
- `v2/ts/packages/fxtr/src/namespace/readNamespace.ts` — read-namespace iface.

## 2. `@fxtr/http-client` + `@fxtr/http-server`

Location: `v2/ts/packages/fxtr-http-client/`, `v2/ts/packages/fxtr-http-server/`.

Ports of the Python experimental HTTP API (see `design/05`):

- `createFxtrApp(scope, pins)` (server, Hono-based) serves `/preview/:id`,
  `/data/:id`, `/pin/*`, `/pins` — the same four routes as the Python ASGI
  app, so the two sides interoperate.
- `HttpReadableArena` / `HttpDeepFetchable` / `HttpFxtrReadNamespace`
  (client) — lazy entity loading over the HTTP API, with the precomp map
  carried in `/data/` responses driving provenance queries.

For the publishing flow, the **client** is the interesting half: the TS build
step could load its input entities over HTTP from a store the build harness
exposes (cf. the `FXTR_REMOTE_BASE_URL` build-time contract pinned in
`../part5_nix_workdir/design_notes/07_remote_entity_store.md`). The TS
**server** is probably not needed (serving is the Python script's job), but it
demonstrates that the wire format is fully implemented on both sides.

Where to look:
- `v2/ts/packages/fxtr-http-client/src/client.ts` — `withHttpReadNamespace`,
  `HttpReadableArena`, precomp handling.
- `v2/ts/packages/fxtr-http-server/src/server.ts` — route shapes,
  `computePrecompClosure`.

## 3. `@fxtr/mdx-transform`

Location: `v2/ts/packages/fxtr-mdx-transform/` (see its
`LOCAL_CODE_OVERVIEW.md`).

Two-stage MDX → FxtrCbor pipeline:

1. A custom JSX runtime (`src/jsx-runtime.ts`): MDX compilation invokes
   `jsx()`/`jsxs()` factories producing `MdxNode` objects; function components
   run server-side (can be async, can create entities in a passed-in scope).
2. Flatten (`src/flatten.ts`): `MdxNode` tree → FxtrCbor tuples
   `[type, props, children]`.

Entry points: `evaluateMdx(source, {components, props})` for runtime
compilation (via `@mdx-js/mdx`), `renderMdxToFxtr(contentFn, opts)` for
pre-compiled MDX modules, and a Vite `jsxImportSource` path for build-time
compilation.

The deprecated consumer (`spikes/old_typescript/example-mdx-rendering/`,
see its `LOCAL_CODE_OVERVIEW.md`) shows the full pattern we want to
resurrect in publishing form: a server compiles MDX pages into
`ComponentTree` entities (with async custom components that mint *other*
entities in the scope and embed weak refs), pins them, and serves them over
fxtr HTTP; a React client walks the tuple tree. The sketch's TS processing
step is essentially this, retargeted: inputs come from pins/layers instead of
being created inline, and outputs go to a layered file store directory
instead of a live HTTP server.

## 4. `fxtr_report_builder` — the *other* MDX path (Python-driven)

Location: `projects/fxtr_report_builder/` (see its `CODE_OVERVIEW.md`).

A self-contained `fxtr-report-builder-cli` TS subprocess (a *fork* of the mdx
transform: own `jsx_runtime.ts` + `flatten.ts`, no `@fxtr/*` deps) bundled to
`dist/main.cjs`, invoked by Python over stdin/stdout with a CBOR payload.
Python defines `RootReport` / `SubReport` entities and calls
`build_root_report(...)`; ref-typed inputs cross the boundary as **opaque**
CBOR tags (TS never interprets them), and Python handles all provenance
projection (`extract_provenance_parent` / `embed_as_provenance_parent`) around
the call. This is what `examples/fxtr_workstation_demo/report_builder_service.py`
uses today, and the resulting entities are what `@fxtr-ui/reports` renders.

This matters for part 7b because it is an existing, working answer to "compile
MDX into report entities" with a very different division of labor than the
sketch's "typescript processing step that … loads entities … and exports them
back as a layered entity":

| | report-builder CLI | sketch's TS step |
|---|---|---|
| driver | Python (subprocess per compile) | nix derivation running node |
| entity I/O in TS | none (opaque tags pass through) | real loads via `@fxtr/core` + writes to a layer |
| provenance | handled in Python around the call | unsolved in TS (not ported) |
| output | Python stores `RootReport` | TS writes a layered-store dir |

Where to look:
- `projects/fxtr_report_builder/fxtr_report_builder/mdx_builder.py` —
  the Python⇄TS envelope + provenance projection.
- `projects/fxtr_report_builder/ts/packages/cli/src/main.ts` — subprocess
  entry; `cbor_passthrough.ts` — tag-preserving cbor-x wrappers.
- `examples/fxtr_workstation_demo/report_builder_service.py` — live usage.

## 5. What's missing for the publishing flow

- **A TS layered-file-store writer** (and possibly reader). The sketch calls
  this out: "maybe duplicating nix layer reader/writer functionality there,
  maybe as a separate package since it depends on filesystem ops". `@fxtr/core`
  is environment-agnostic; a layer writer needs `node:fs`, so a separate
  package (e.g. `@fxtr/layered-store-node`) fits the existing layering. See
  `design/02_layered_file_store.md` for the format it must produce and
  `discussion_topics.md` for the open choice between "full writer in TS" vs
  "TS emits a simple intermediate, Python rewrites it into a layer".
- **A TS path for *loading* build inputs inside a nix derivation.** Options
  sketched in `design/06_proposed_flow.md`: read parent layers directly from
  disk (needs a TS layer *reader*), or HTTP via `@fxtr/http-client` against a
  build-time rehost (the `FXTR_REMOTE_BASE_URL` contract), or a hybrid.
- **Provenance story.** If published entities carry provenance (likely, since
  workstation-produced data does), the TS step either needs the
  provenance-parent sidecar ported, or needs to treat layered ids the way the
  report builder does (opaque pass-through + Python-side projection).
- **Freshness.** `v2/ts` is flagged as lagging the current fxtr2 design;
  expect to budget for small fixes when first exercising it against
  Python-written stores.
