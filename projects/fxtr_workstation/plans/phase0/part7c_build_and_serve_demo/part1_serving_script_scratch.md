# 7c.1 scratch — flake-built-renderer serving (Option A) findings

Working notes behind the revised `part1_serving_script.md`. Not a spec; just the
evidence + file locations that informed it.

## Decision context

Renderers for the publishing demo are **not** flake-incidental — flake-built
renderers are coming to the publishing system, so we generalize now: `fxtr_nix
serve` serves entities + namespaces + a **flake-driven** dynamic-dispatch service.
The demo becomes "a flake + a frontend", no custom backend. (Option A from the
chat scoping; the alternative demo-specific backend was rejected as it wouldn't be
reusable.)

## What already exists (the flake + nix side is general)

- `nix/lib/esbuildRenderer.nix` — `{ src, externals?, name?, system? }` → a
  derivation producing a single `.mjs` (ESM bytes), via `pkgs.esbuild ... --bundle
  --format=esm --platform=browser --jsx=automatic`. Externals default to
  `workstationExternals` (matches the frontend import-map shim).
- `nix/lib/mkUiPreset.nix` — transposes per-renderer declarations into a
  type-id-keyed table: `{ "<type-id-string>" = { typeEntity; byDispatchKey = {
  "<uuid>" = { subtypeKey; renderer; }; }; }; }`. Pure structural transpose;
  detects duplicate `(type-id, dispatch-key)` claims.
- Flake convention: each renderer dir ships a `fxtr_renderer.nix` slice; the root
  flake merges them and exposes `fxtr.workstation.uiPresets.<name> = lib.mkUiPreset
  rendererEntries;`. Example: `examples/fxtr_workstation_demo/agent_workdir/`
  (`flake.nix:81-99`, `renderers/*/fxtr_renderer.nix`).

## The dispatcher build/serve logic (decoupled from the workstation server)

`projects/fxtr_workstation/src/fxtr_workstation/dynamic_dispatch/`:

- `dynamic_dispatch_types.py` — `RendererEntity {module: bytes}` (`:67-82`),
  `ReloadEverythingNotification` (empty entity), `RendererResolution {subtype_key,
  renderer}` (`:85-96`), `WorkstationDispatcherProto`/`NixDispatcherProto`
  (`:120-236`), and the type-id constants (`DYNAMIC_DISPATCH_NS`,
  `RENDERER_ENTITY_TYPE_ID`, `RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID`,
  `WORKSTATION_DISPATCHER_TYPE_ID`).
- `nix_configurable_dispatch/nix.py` — `_PRESET_APPLY` (`:60-76`),
  `eval_preset_table(handle, git_commit, flake_attr, flake_path)` (`:103-125`),
  `build_drv_outputs(handle, drvs)` (`:128-156`). Depend only on a
  `fxtr_nix_runner` `Handle` (run_nix/materialize) — no workstation server.
- `nix_configurable_dispatch/service_impl.py` — `set_preset` (`:196-275`): eval →
  batched build → validate each typeEntity id == preset key (`:277-306`, via
  `LayeredFileStoreReader`) → read `.mjs` → `RendererEntity(...).store(scope)` →
  build resolve table → broadcast reload. `resolve` is a pure dict lookup
  (`:309-322`); `subscribe` uses a `SubscriberHub` (`:325-333`).
- Coupling verdict: depends only on `fxtr2` (scope/entity/subscription/layered
  reader) + `fxtr_nix_runner` transport. No `/rpc`, no `WorkstationState`, no
  server lifecycle. Caller owns scope + task group.

## The stateless-HTTP dispatcher wire contract (what the connector needs)

From `examples/fxtr_http_demo/server/demo_backend.py:237-321` +
`v2/fxtr2/experimental/http/`:

- `resolve(args=[type_ref: FxtrID, dispatch_key_uuid: str], kwargs, ctx) ->
  {"subtype_key": str, "renderer": EntityRef} | None`.
- `subscribe(args=[inbox], kwargs, ctx) -> ctx.transient({"unsubscribe": ...})`;
  `ctx.notify(inbox, reload_ref)` pushes a `ReloadEverythingNotification` ref.
- Register: `reg.add_ws_service("dispatcher", ws_path="/ws", impl={resolve,
  subscribe, "$fxtr.type": ...}, type_id=<dispatcher type id>,
  discovery_name="dispatcher")`. Renderer `.mjs` blobs are normal content-addressed
  entities served via `/data`.
- Frontend (`fxtr-ui-connector-http/src/connector.ts:437-478`,
  `fxtr-ui-react-dispatch/src/dynamic_dispatcher.ts:15-56`): discovers the
  `dispatcher` ws service via `/discovery`, calls `resolve(typeRef, key.uuid)`,
  caches, loads the renderer entity bytes, compiles, indexes
  `module[subtype_key][key.uuid]`; subscribes an inbox for reload broadcasts.

## Layering obstacle + the move

`fxtr_workstation` sits **above** `fxtr_nix_runner`. So the runner can't import the
renderer types or the preset helpers today. Option A relocates **down**:

- entity types + type-id constants → `fxtr2` (content-addressed by fixed UUIDs →
  no id change → wire-compatible; frontend unaffected).
- preset nix helpers → `fxtr_nix_runner` (they only need a `Handle`).
- `RendererResolution` + protos + impls stay in `fxtr_workstation`, importing the
  types from `fxtr2`.

## Prior pass — preserved in jj snapshot `wrrtusnz`

A first pass (before the flake-renderer direction) implemented the
entity/namespace serve and got it green on the host backend; that working copy was
then reset to plans-only so the plan reads from a clean slate. The code is
preserved in jj change **`wrrtusnz`** ("wip: 7c.1 full implementation snapshot") —
restore individual files from it during implementation rather than rewriting:

- `FlakeManager.list_namespace_names` + `namespaces.eval_namespace_names`.
- `serve.py` `published_serving_context` (entities + namespaces, provenanced; **no
  dispatcher, no `configure` hook**) + `PublishedServer` + `run_published_server` +
  the `fxtr_nix serve` CLI.
- `tests/test_serve_e2e.py` + `tests/_workdir_templates/publishing_demo/` (entity +
  namespace only — needs the `uiPresets` renderer + dispatcher assertions added).
- CODE_OVERVIEW edits reframing `fxtr_nix_runner` as the project-state manager.

Not in that pass (the new work): the relocations, `renderers.py`, the dispatcher ws
service in `serve.py`, and the fixture/E2E renderer additions.
