# Stage 7 post-1 — User-supplied external services

Goal: let users attach arbitrary services to a `WorkstationServer`
at construction time, alongside the stage-7 persistence caps.
Today the only "system-sourced" services the workstation can mount
are persistence caps (each backed by a `FxtrNamespace`). This stage
generalizes that machinery so users can pass any
`(ServiceRef, FxtrScope)` pair plus an external name and have the
workstation expose it the same way: a system-sourced
`Registration` reachable via `get_service_by_external_name`,
visible in the `/client` services snapshot, surfaced in the debug
UI as a `system:external` chip.

Design notes that don't fit in the plan body live in
[`stage7post1_external_services_scratch.md`](./stage7post1_external_services_scratch.md).

## Settled design

- **Parallel to persistence, not a replacement.** External-services
  configs sit alongside `persistence_namespaces` on
  `WorkstationServer.__init__`. They ride the same `SystemSource`
  registration channel (stage 7a) and the same shutdown sweep
  (stage 7b), with their own kind tag (`"external"`). See "Future
  unification?" below.
- **User retains lifetime ownership of the scope.** The user opens
  a `FxtrScope` themselves (typically already populated with the
  service ref via `mount(...)` or because some upstream code put it
  there) and hands it to the workstation. The workstation pins the
  scope for its own lifetime via a `bridge_service_from_scope` call
  on `root_scope`; on shutdown that pin releases and the user's
  scope is free to drain.
- **Bridge into `root_scope`, then mint via the standard helper.**
  We call `state.root_scope.bridge_service_from_scope(ref,
  user_scope)` to install the ref on `root_scope`. Then we hand
  `(ref, root_scope)` to `get_or_create_service_record`, which
  spawns the standard `service_record_worker` to bridge from
  `root_scope` into the per-record `service_scope`. This is the
  same lifetime model the user described:

  > use `bridge_from_scope` to bridge just the requested service
  > into the root scope. this will automatically keep the service
  > open and available to the root scope.

- **No support for entities yet.** Symmetric system-sourced
  entity-registrations would require widening
  `EntityRegistration.source` (currently RPC-only) and adding a
  parallel attach flow. Out of scope; revisit when there's a
  concrete need. The 7a impl-notes already flag this as the trigger.

## Out of scope

- Entities (see above).
- Override-able `SystemSource.kind` per config — every external
  service gets `kind="external"` for now.
- Debug-UI changes — the existing stage-7d
  `system_source_kinds` chip already renders any kind value, so
  `system:external` shows up automatically.
- Concurrency tests / shutdown-mid-call tests — not contract
  behavior, same call as we made in 7d.

## Why no worker

The persistence cap needs a worker because it owns an `impl_scope`
(where it mounts the persistence impl onto a `FxtrScope` whose
lifetime has to span the workstation's). External services have no
such per-config scope to own — the user already owns the source
scope, and the only thing the workstation needs to keep alive is a
single `bridge_service_from_scope` pin installed on `root_scope`,
which `root_scope`'s own drain (at workstation shutdown) releases
automatically. So bring-up collapses to sequential inline work at
startup; teardown rides the existing system-source release sweep
plus `root_scope.aclose`.

## Moving parts

### `external_services.py` (new)

- `ExternalServiceConfig` — frozen dataclass:
  - `external_name: str` — must be unique across both the external
    service list and the persistence namespace list.
  - `service: ServiceRef[Any]` — the ref the workstation should
    expose under `external_name`.
  - `scope: FxtrScope` — a scope where `service` is reachable
    (registered there or in an ancestor). Doesn't need to be
    mountable. The workstation pins this scope until shutdown.
- `attach_external_service(state, config) -> None` — async helper
  called by `WorkstationServer.__asynccontextmanager__` once per
  config during startup. Steps:
  1. `await state.root_scope.bridge_service_from_scope(config.service,
     config.scope)`. Pins `config.scope` to `root_scope` for the
     workstation's lifetime. Errors here propagate to the caller
     (which is mid-`__aenter__`); the unwind drains `root_scope`
     and any earlier bridges release.
  2. Inside `state.mutation_context()`, call
     `get_or_create_service_record(state, config.service,
     state.root_scope)`. Standard helper — spawns the per-record
     `service_record_worker` on `state.background_tg` to bridge
     from `root_scope` into the per-record `service_scope`.
  3. `await tracked.bridge_ready.wait()`. On `bridge_error`,
     release the just-minted record via
     `maybe_release_service_record` (inside `mutation_context`)
     and re-raise.
  4. Inside `mutation_context`, install a `Registration` with
     `source=SystemSource(kind="external",
     metadata={"external_name": config.external_name})` and
     `external_name=config.external_name`. The same defensive
     "external_name already taken" check the persistence worker
     does — startup-only race against an RPC peer; on conflict,
     release the just-minted record and raise.

### `server.py`

- New parameter on `WorkstationServer.__init__`:
  `external_services: list[ExternalServiceConfig] | None = None`.
  Stored as `self._external_service_configs`.
- Refactor `_validate_persistence_configs` →
  `_validate_system_service_configs(self) -> None`:
  - Walk `self._persistence_configs ∪ self._external_service_configs`
    and reject any duplicate `external_name` across the union (same
    error message shape, just sourced from a single check).
  - Mode validation stays scoped to persistence configs.
- `__asynccontextmanager__`:
  - After persistence-cap startup
    (`await background_tg.start(persistence_cap_worker, state, cfg)`
    per persistence config), add a sequential loop:
    `await attach_external_service(state, cfg)` per external-
    services config. No `background_tg.start` — there's no
    long-lived task to spawn.
  - Order: persistence first, then external. The order doesn't
    matter functionally, but a consistent rule makes tests
    predictable.
  - Existing shutdown sweep is unchanged — it walks every
    `SystemSource`-sourced registration regardless of kind, and
    the `service_record_worker` for each external service drains
    its `service_scope` the same way the persistence one does.

### `__init__.py`

Re-export `ExternalServiceConfig` next to `PersistenceNamespaceConfig`.

## Tests (`tests/test_external_services_7post1.py`, new)

All using `connect_rpc(ws.rpc_url)` for the inbound peer; the
external service is a freshly-mounted `EchoImpl` from the existing
`_fixtures.py`. Each test opens a `FxtrScope` to hold the impl,
mounts it, and hands `(ref, scope)` to the server.

- **Round-trip.** One external service. RPC peer resolves
  it via `get_service_by_external_name`, invokes a method, asserts
  the response.
- **Two external services co-exist.** Two configs with distinct
  external names. Both reachable, both invocable.
- **Mixed external + persistence in the same server.** One
  external + one persistence config. Both reachable. The
  ``services`` snapshot shows the expected `system_source_kinds`
  per record (`["external"]` vs `["persistence"]`).
- **Duplicate external_name across external + persistence ⇒
  rejected at construction.** Exercises the unified validator.
- **Duplicate external_name within external_services ⇒ rejected
  at construction.**

(Skipping concurrency / shutdown-mid-call tests intentionally —
matches stage 7d.)

## Implementation steps

- [x] **post1.1** — Added `ExternalServiceConfig` (frozen
      dataclass: `external_name`, `service`, `scope`) and the
      `attach_external_service(state, config)` async helper in
      `external_services.py`. Module-doc walks the
      bridge-into-root_scope lifetime model and explains why no
      worker (no per-config scope to own — the only lifetime
      claim is the bridge on `root_scope`, which `root_scope`'s
      drain releases). Helper mirrors the
      `persistence_cap_worker` mint/await/install/rollback shape
      verbatim, with `kind="external"` and metadata carrying
      just `external_name`.
- [x] **post1.2** — `server.py`: renamed
      `_validate_persistence_configs` →
      `_validate_system_service_configs` (combined uniqueness
      check across persistence + external lists; persistence-mode
      check stays in the persistence-only branch). Added
      `external_services` parameter and the inline `await
      attach_external_service(...)` loop after the persistence-cap
      bring-up loop. `ExternalServiceConfig` re-exported from
      `__init__.py`. Persistence tests still green.
- [x] **post1.3** — `tests/test_external_services_7post1.py`:
      five tests covering round-trip, two co-existing, mixed
      external + persistence (asserts ``Registration.source.kind``
      per record), duplicate external_name across
      external/persistence rejected at construction, duplicate
      external_name within external rejected at construction.
      Full suite: 304 passed (was 299 + 5 new).
- [x] **post1.4** — Updated `CODE_OVERVIEW.md`: new
      `external_services.py` Module Breakdown entry; the
      `WorkstationServer` description now mentions
      `external_services=` and the unified validator
      (`_validate_system_service_configs`); shutdown ordering
      paragraph extended to cover the bridge-release on
      `root_scope.aclose`; `__init__.py` re-export list grew
      `ExternalServiceConfig`; the test-file listing gained
      `test_external_services_7post1.py`.

## Future unification?

`persistence_cap_worker` and `attach_external_service` share the
"mint record + register SystemSource" tail. They differ in:

- Persistence: long-lived worker that owns `impl_scope`, mounts
  the impl on it, parks on `shutdown_event`. Mint uses
  `(impl_ref, impl_scope)`.
- External: inline bring-up. Bridge into `root_scope`; mint uses
  `(user_ref, root_scope)`. No long-lived task.

The shared tail is a 6–8 line block (mint, await bridge, install
Registration with rollback). A helper of the form
`_install_system_sourced_registration(state, source_ref,
source_scope, external_name, kind, metadata) -> None` could
absorb it. Not worth doing now — the duplication is small and
pre-emptively collapsing it would lock in a contract before we
know whether a third caller (e.g. user-supplied entities) wants
to reuse it. If a third caller appears, factor out then.

## Notes / open follow-ups

- **`service: ServiceRef[Any]`** typing. The plan uses `Any` for
  the type parameter because the workstation can't statically
  know what protocol the user is exposing. Callers reaching it via
  `get_service_by_external_name` get back a `ServiceRef[Any]` and
  must `replace_service_type(...)` themselves. Same as the
  persistence caps today, just with no canonical protocol.
- **Bridge errors at startup.** If the user's scope doesn't
  actually contain the ref, or the scope is closed, or the ref is
  already registered on `root_scope` (e.g. duplicate config with
  the same ref), `bridge_service_from_scope` raises. The error
  surfaces through `background_tg.start(...)` to
  `WorkstationServer.__aenter__`, which propagates cleanly. No
  rollback needed — the worker hasn't minted a record yet at that
  point.
