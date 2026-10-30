# Stage 5 (was stage 4) — HTTP connector

A second connector implementing the same `Session` interface,
backed by the existing fxtr HTTP API instead of a workstation
WebSocket. Validates the connector abstraction by reusing the
stage-4 (was stage-3) example app against an HTTP server.

## What gets built

- `fxtr-ui-connector-http` package.
- HTTP-backed `Session`: degenerate (single per connector, or
  one per "open root") — no callbacks, no invokes, no services.
- Pin-by-name / pin-listing surfaced as ambient connector ops
  (similar to `HttpFxtrReadNamespace`'s `listPins`).
- `Session.allocateLoad(stub)` translates to a fetch against
  `/data/:id_string` and lands the decoded entity in the local
  `slotFills` map.
- CBOR decoder that produces the same structured shape as the
  workstation decode (stub instances at leaves).
- Match the slot lifecycle: a slot stays alive while referenced;
  released when the last reference drops.

## Deliverable

The stage-3 example app, retargeted to an HTTP server, with the
same component code. Demonstrates the connector abstraction
holds across backends.

## Design notes to consult

- `design_notes/02_connector_abstraction.md` — HTTP-as-degenerate
  -session pattern.
- `design_notes/06_no_schema_for_now.md` — decoder produces
  structured state with stubs at leaves; the CBOR walk follows
  the same pattern as the JSON-list-escape walk.

## Open questions / points to discuss

- **HTTP entry-point shape.** `connector.createSession({ pin:
  "demo/section" })` vs. `createSession({ entityIdString: ... })`
  vs. exposing `listPins()` as an ambient op. Probably the
  latter, with sessions being created with explicit content.
- **CBOR decoder reuse.** The decoder needs to take fxtr CBOR
  (with the v2 wire tags 33000 / 33001 / 33002) and produce the
  same in-memory shape as the workstation JSON decoder. We can
  either fork or share — should fit in `fxtr-ui-core` since
  both connectors need it.
- **Slot ints for HTTP.** The workstation uses session-scoped
  monotonic integers. For HTTP we have no session on the wire,
  so we just allocate locally. Confirm that's fine.
- **Service / callback ops on an HTTP session.** Should raise
  with a clear error. Test the boundary.
