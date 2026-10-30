# Part 5 — sandboxed (nix-built) stateless service provider (stub)

> Stub plan. **Re-read `fxtr_sandboxing`'s spawn/connect machinery before
> scoping this in detail** — the wiring below is the intended shape but may
> shift once we see the real connect path. Captures boundaries + what's
> involved.

Adds `SandboxedStatelessServiceProvider` (in `projects/fxtr_sandboxing`, new
module): everything the basic provider does, plus turning a nix git-flake
reference into a hermetic, sandbox-spawnable service. After this part you can
spin up a whole workflow from a git-pinned entrypoint instead of bridging in
external services by hand — the usable end state. Design:
[design/stateless_service_provider.md](design/stateless_service_provider.md).

## What's involved

- **Handle entities** (1-1 with the nix attrset discriminator, no redundant
  fields): `NixWasmStatelessServiceHandle` (carries `entry_point`) and
  `NixContainerStatelessServiceHandle` (no `entry_point`; baked into image).
  Both carry `drv_hash`, `config: EntityRef | None`, `bindings: dict[str,
  EntityRef[handle]]`. Each gets a provenance-parent c-node `{git_commit,
  flake_path, flake_attr}` at registration (not consulted for caching).
- **Nix attrset schema + `nix/` helpers**: a new `kind =
  "fxtr-stateless-service"` attrset (`runtime`, `service` drv, `entryPoint`,
  `config`, `bindings`) alongside the existing
  `NixWasmServiceDefn`/`NixContainerServiceDefn`; helpers in root `nix/` produce
  it from a user package + entrypoint.
- **Registration walk** `register_flake_service(git_commit, flake_attr,
  flake_path=None)`: depth-first `nix eval` over the attrset (bindings first),
  dispatch on `kind`/`runtime` to the matching handle type, hash the principal
  drv, store config as an entity, mint handle + provenance c-node, record
  `drv_hash -> (git_commit, flake_attr_path, flake_path)` in a **source map**.
- **Spawning** (`spawn` dispatches on handle type): look up the source map by
  `drv_hash`; get-or-create a running sandbox from a **spawned-sandbox cache**
  honoring `max_guests_per_sandbox` (total-connects budget for v0; concurrent
  counting + pooling is a followup); `connect()` with the composite config;
  return the ref. `release_cached_sandboxes()` drops the cache.
- **Guest seam (reuse, no new wire kind — OQ-8)**: reuse the existing
  `SandboxConnectProto.connect(config)` path *and* guest adapter. Expose a
  `StatelessServiceConfig(config, bindings, spawner)` dataclass; a user
  entrypoint annotated to receive it decodes the composite config and is called
  directly. Bindings are resolved guest-side on demand via
  `spawner.spawn(binding_handle)` (v0 hands guests the provider's full spawner;
  restricting to declared bindings is a followup).
- **Both runtimes** (wasm/eryx + container) exercised.

## Boundary

- Implements the same `StatelessServiceProviderProto` /
  `StatelessServiceSpawnerProto` surface as the basic provider, so the runner
  (parts 2–4) consumes it unchanged.
- Lives in `fxtr_sandboxing` (own package/overview); integration tests gated
  like the existing sandbox tests.

## Open / to-confirm at scope time

- Exact reuse of `fxtr_sandboxing`'s connect/provenance machinery (the "another
  look" flag).
- OQ-7 handle field sets are settled (per-kind entity types); confirm nothing
  else in the attrset needs to participate in identity.
- Followups that touch here: provenance-from-handle for spawned services,
  binding authorization via provenance c-nodes, concurrent-guest accounting.

## Tests (sketch)

Register + spawn a wasm service and a container service from a git flake ref;
bindings resolved guest-side; `max_guests_per_sandbox` rollover;
`release_cached_sandboxes`; a task fn spawned hermetically runs end-to-end
through the runner.
