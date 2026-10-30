# Remote entity store wiring

How nix-side preloaders fetch entities from a "remote entity store" —
in v0 a no-op stub, eventually an actual hosted service. Most of
this is forward-looking; v0 just needs the contract pinned so we
don't paint ourselves into a corner.

> **Revised (2026-05-30):** this is a **DATA-only** concern, now
> unambiguously so — code (services, renderers) travels through **git**,
> not through any entity store, so the remote-store work never touches
> the code path. Nothing else in this note changes.

## Three target states

### v0: self-contained, no preload
Every layer derivation builds every entity it depends on from
scratch. No FOD-style derivations. No env vars consumed. The flake
ecosystem works end-to-end without ever touching a remote store.

### Intermediate: build-time remote, runtime workstation store
- The workstation server has a "remote entity store" registered (as
  an external service per
  `projects/fxtr_workstation/external_services.py`).
- For the duration of a `nix build`, the MCP exposes the
  workstation's store on a localhost HTTP endpoint (via a small
  on-demand server it spawns alongside the build). The localhost
  URL + a one-shot bearer token are passed to the build as
  impureEnvVars.
- FOD preloader derivations read the env vars, HTTP-fetch from the
  endpoint, write the fetched entities into a layered-file-store
  output. Output hash is pinned so nix trusts the result.
- At sandbox execution time, layers retrieved via
  `retrieve_nix_entity` are read with a layered-file-store reader
  whose remote-fallback path is a custom in-memory `DeepFetchable`
  that goes back to the workstation backend (same service, different
  transport — direct in-process rather than HTTP).

### Eventual: hosted remote
The workstation and the build both talk directly to a real hosted
service. The MCP-rehost step is dropped. Out of scope for now.

## Build-time env-var contract (resolved)

- `FXTR_REMOTE_BASE_URL` — required when the FOD wants to reach a
  remote. Base URL of the HTTP endpoint. Includes scheme and port,
  no trailing slash. Path conventions TBD.
- `FXTR_REMOTE_BEARER_TOKEN` — optional. When set, the FOD sends
  `Authorization: Bearer <token>`. When missing/empty, no auth
  header.

Two env vars rather than one JSON envelope: simpler to consume, more
nix-idiomatic, and the FOD's compiled-in code can't parse a new
protocol version anyway. If we ever need extensibility, we'll add a
third env var.

## MCP-side localhost rehost (intermediate state)

Open questions:

- Q7.A: Does the rehost serve on a fixed port the MCP picks per
  build, or does it bind to ephemeral + emit the URL? Ephemeral
  feels safer. => Decision: ephemeral.
- Q7.B: Is the bearer token a fresh `secrets.token_hex` per build,
  revoked when the build process exits? => Decision: yes.
- Q7.C: Where does the rehost server live in the code? A small new
  module under `fxtr_workstation_mcp/`, or part of the existing
  workstation server?
- Q7.D: What's the FOD-side API contract? Probably a small fxtr2
  remote protocol (see `fxtr2/data/remote/remote_client.py`)
  re-exposed over HTTP — same shape as the existing
  `fxtr2/experimental/http/` server but bridged through the
  workstation's store via the registered "remote entity store"
  external service.

## In-memory DeepFetchable for sandbox-side reads

When the sandbox opens an entity via `retrieve_nix_entity`, layers
that contain `mark_as_remote` pointers need a fallback for fetching
the actual bytes. In the intermediate state, that fallback is a
fresh `DeepFetchable` implementation that talks to the workstation's
"remote entity store" service in-process (no HTTP). Reuses the
fxtr2 remote-fetch protocol; the workstation external service
provides the backend.

- Q7.E: Is this a subclass of the existing
  `fxtr2.data.remote.remote_client.RemoteClient` protocol or a
  brand-new class? => Tentative decision: probably a different class.

## v0 scope

For the first implementation, we **do not** implement either the
MCP-side rehost or the in-memory DeepFetchable. We just:

1. Ship the env-var contract in nix (writers use it iff set).
2. Build everything from scratch in each layer derivation.
3. Make sure the design doesn't preclude wiring in (1)+(2) later.

Concretely: layer-writer derivations should be authored so that
adding the remote-fetch step later is a small change to the writer,
not a re-architecture.
