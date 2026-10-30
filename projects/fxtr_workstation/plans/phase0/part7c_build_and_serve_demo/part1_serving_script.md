# Part 7c.1 — general published-flake serving (entities, namespaces, flake-built renderers)

> Status: scoped, ready to implement.

## Goal

A **general** serving capability in `fxtr_nix_runner` that takes a published flake
dir and serves its whole UI over the **7a stateless HTTP backend**, with
**provenance exposed** — nothing demo-specific:

- **entities** (`fxtr.entities.*`) — layered `(data, prov)` ids on the wire for
  provenanced layers;
- **namespaces** (`fxtr.namespaces.*`) — bare `"<ns>:<key>"` pins (the tsv-namespace
  flow is provenance-stripping);
- **a flake-built dynamic-dispatch service** — renderers built from the flake's
  `fxtr.workstation.uiPresets.<name>` (the same preset mechanism the workstation
  uses), so a frontend can resolve a renderer per entity type.

Exposed as `published_serving_context(...)` (a library async CM, mirroring
`fxtr2.experimental.http.server.serving_context`) plus a `fxtr_nix serve
<flake_dir>` CLI. Because it's flake-agnostic, the publishing demo (7c.2) is then
just *a flake* (entities/namespaces + `uiPresets` renderers) *plus a web frontend*
— no demo backend.

## Why this lives in `fxtr_nix_runner` (+ the layering move)

`fxtr_nix_runner` already owns the flake manager (repo store + build + the
entity/namespace read adapters), so serving the built result belongs next to it.
The renderer dispatcher logic is decoupled from the workstation *server* (it needs
only `fxtr2` + the nix transport — see `part1_serving_script_scratch.md`), but two
pieces it needs currently live in `fxtr_workstation`, which sits **above**
`fxtr_nix_runner` in the dep graph. So this part **relocates them down a layer**
(§Relocations) before the runner can serve a flake-driven dispatcher.

## The manager API it drives

`projects/fxtr_nix_runner/src/fxtr_nix_runner/manager.py`:

- `async with FlakeManager(repo, *, rev=None, backend=None, isolate_scope=False)
  as mgr:` — opens the repo store + a long-lived manager scope (`mgr.scope`).
- Read adapters need an active **build span** (builds via the wired-up rehost):
  `async with mgr.build_span() as span: …`.
- **Entities** (`EntitiesReadAdapter`): `list_entity_names(prefix="")` (cheap eval,
  no build); `retrieve(name, …)` builds + materializes the layer, mounts the pin
  into `mgr.scope`, returns a `FxtrID`/`EntityRef` — **layered** for a provenanced
  layer.
- **Namespaces** (`NamespaceReadAdapter`): `build_namespace_manifest(span, rev,
  name)` → a `key → FxtrID` manifest; `async with mgr.namespace(span, name) as ns:`
  for a `FxtrReadNamespace`. Per-retrieve is pure repo-store lookups.
- **Repo store**: `mgr.repo_store` → a `(DeepFetchable, Lease)` pair to mount on the
  serving scope via `register_deep_fetch_source(...)`.

**New manager methods this part adds:**

- `list_namespace_names(span, prefix="")` — eval `f: builtins.attrNames
  (f.namespaces or {})` (mirrors `list_entity_names`; backed by a module helper
  `eval_namespace_names` in `namespaces.py`).
- `list_preset_names(span, prefix="")` — eval `f: builtins.attrNames
  (f.workstation.uiPresets or {})` (backed by `eval_preset_names` in the new
  `renderers.py`).
- A renderer read adapter (`renderers.py`, §"Renderer adapter") that builds a preset
  into a servable table.

## The 7a serving substrate it assembles

`v2/fxtr2/experimental/http/`:

- `create_fxtr_app(scope, pins, *, provenanced=False)` — entity endpoints
  (`/data/{id}`, `/pin/{name}`, `/pins`, `/preview/{id}`). With `provenanced=True`,
  layered pins emit `[data, prov]`.
- `ServiceRegistry(scope, *, base_url=None, provenanced=False)` — `add_entity(name,
  id)` registers an entity for `/discovery`; `add_ws_service(routing_name, *,
  ws_path, impl, type_id, discovery_name)` registers a multiplexed ws service;
  `routes()` + `discovery_handler` mount alongside the entity app. We pass **no
  `base_url`** (relative discovery URLs — the client resolves them against the
  origin it reached).
- A ws service `impl` is a method-map `{method: async (args, kwargs, ctx) -> val}`;
  `ctx.notify(inbox, payload)` pushes to a callback inbox, `ctx.transient(impl)`
  returns a connection-scoped service. (This is the shape the dispatcher uses.)

## Relocations (enabling work)

1. **Dynamic-dispatch entity types → `fxtr2`.** Create `fxtr2/experimental/dynamic_dispatch.py`
   (sibling to `experimental/http`) holding `RendererEntity {module: bytes}`,
   `ReloadEverythingNotification`, and the type-id constants (`DYNAMIC_DISPATCH_NS`,
   `RENDERER_ENTITY_TYPE_ID`, `RELOAD_EVERYTHING_NOTIFICATION_TYPE_ID`, and the
   dispatcher service type id). Currently in
   `fxtr_workstation/.../dynamic_dispatch/dynamic_dispatch_types.py`. They're
   content-addressed by **fixed UUIDs**, so moving the definitions changes no ids —
   wire + frontend + existing workstation behavior unaffected.
   `fxtr_workstation.dynamic_dispatch` re-imports them (and re-exports for
   back-compat); `RendererResolution`, the dispatcher protos, and the dispatcher
   impls **stay** in `fxtr_workstation`, importing the types from `fxtr2`.

2. **Preset nix helpers → `fxtr_nix_runner`.** Move `eval_preset_table` /
   `build_drv_outputs` / `_PRESET_APPLY` (from
   `fxtr_workstation/.../nix_configurable_dispatch/nix.py`) into the runner (manager
   tier — they only need a nix `Handle`). `fxtr_workstation`'s
   `nix_configurable_dispatch` imports them back from the runner (it already depends
   on the runner), keeping the typeEntity-id validation single-sourced.

> Both relocations are import-site refactors, not behavior changes — keep
> `fxtr_workstation`'s test suite green.

## Renderer adapter (`fxtr_nix_runner/renderers.py`)

A new manager-tier module, mirroring `entities.py` / `namespaces.py`:

- `eval_preset_names(span, rev, prefix="")` → the `fxtr.workstation.uiPresets` attr
  names (cheap eval). Surfaced as `FlakeManager.list_preset_names`.
- `build_preset_table(span, manager, preset_name) -> RendererTable` — eval the
  preset (drv paths, no build) → one batched `nix build` of the renderer +
  typeEntity drvs → validate each typeEntity's id matches its preset key → read each
  renderer's `.mjs` bytes, mint `RendererEntity(module=bytes)` into `mgr.scope` (so
  `/data` serves them through the serving scope's `include`) → return

  ```
  RendererTable = dict[str, dict[str, RendererEntry]]   # type_id_string -> dispatch_key_uuid -> entry
  RendererEntry = (subtype_key: str, renderer_id: FxtrID)
  ```

  Reuses the relocated `eval_preset_table` / `build_drv_outputs` (called with
  `span.handle`; preset + typeEntity builds are pure, so no rehost env needed).

## Serving structure

```
async with FlakeManager(repo, rev=rev) as mgr:
    async with mgr.build_span() as span:                          # build phase
        ents = mgr.entities(span)
        entity_ids = {n: await ents.retrieve(n)                   # layered ids
                      for n in await ents.list_entity_names()}
        ns_manifests = {ns: await build_namespace_manifest(span, rev, ns)
                        for ns in await mgr.list_namespace_names(span)}
        preset = await pick_preset(span, mgr)                     # default/sole, or --preset
        table = await build_preset_table(span, mgr, preset) if preset else None

    # serve phase. Entities mounted into mgr.scope by retrieve; renderer blobs
    # minted into mgr.scope by build_preset_table; serving scope include=[mgr.scope]
    # + the repo-store source resolve everything via /data. All outlive the span.
    async with FxtrScope(isolated=True, include=[mgr.scope]) as serving_scope:
        store, lease = mgr.repo_store
        serving_scope.register_deep_fetch_source(store, IndependentLocalBridge(lease=lease))
        pins = dict(entity_ids)                                   # entities: layered
        for ns, manifest in ns_manifests.items():
            for k, fid in manifest.items(): pins[f"{ns}:{k}"] = fid   # namespaces: bare
        async with anyio.create_task_group() as tg:
            app = create_fxtr_app(serving_scope, pins, provenanced=True)
            reg = ServiceRegistry(serving_scope, provenanced=True)
            for name, fid in entity_ids.items(): reg.add_entity(name, fid)
            if table is not None:
                register_dispatcher(reg, serving_scope, table)   # the ws dispatch service
            # mount app.routes + reg.routes() + /discovery; hypercorn on tg; yield base_url.
```

**The dispatcher ws service** (`register_dispatcher`): a method-map registered via
`reg.add_ws_service("dispatcher", ws_path="/ws", impl={...}, type_id=<dispatcher
type id>, discovery_name="dispatcher")`:

- `resolve(type_ref, key_uuid)` → `RendererTable` lookup → `{"subtype_key",
  "renderer"}` or `None` (NoMatch).
- `subscribe(inbox)` → `ctx.notify(inbox, reload_ref)` (a `ReloadEverythingNotification`
  stored in the scope) then `ctx.transient({"unsubscribe": …})`. Static serve: the
  reload fires on connect; file-watch hot-reload is a follow-up.

The renderer `RendererEntity` blobs resolve through `/data` (they're in `mgr.scope`).

## Decisions

- **Lazy entity serving — no copy.** `/data`'s deep-fetch walks parent stores, so
  `include=[mgr.scope]` resolves entities without an eager closure copy.
- **Build strategy: lazy.** Retrieve each declared entity on demand; build the
  preset once. Batched eval-once/build-many is a post-v0 optimization.
- **Pin-name scheme `"<ns>:<key>"`.** Safe: `/pin/{name:path}` is a wildcard route;
  the connector treats discovery/pin names opaquely; nothing splits on `:`.
- **Provenance.** `provenanced=True` + layered entity ids (the entities adapter
  yields them for provenanced layers); namespace ids stay bare.
- **No `base_url` on the registry** — relative discovery URLs, resolved by the
  client against the origin it reached (robust behind a proxy / non-loopback host).
- **`--rev` flag** selects committed-rev vs live-workdir reads.
- **`--preset NAME` flag**; default to the sole declared preset, or one named
  `default`; if the flake declares no `uiPresets`, omit the dispatcher (entities +
  namespaces still served).
- **Preset helpers: move (share), not reimplement** — avoids duplicating the
  typeEntity-id validation; `fxtr_workstation` already depends on the runner.
- **`fxtr2` is the home for the moved entity types** (`experimental/dynamic_dispatch.py`,
  sibling to `experimental/http`) — they're the backend side of a frontend serving
  protocol, so they belong with the rest of the serving stack, below both consumers.

## Implementation steps

- [x] Relocate the dynamic-dispatch entity types + constants →
      `fxtr2/experimental/dynamic_dispatch.py`; re-point `fxtr_workstation` imports
      (+ back-compat re-export); workstation tests green.
- [x] Relocate the preset nix helpers → `fxtr_nix_runner` (manager tier,
      `renderers.py`); re-point `nix_configurable_dispatch`; workstation tests green.
- [x] `FlakeManager.list_namespace_names` (+ `namespaces.eval_namespace_names`).
- [x] `fxtr_nix_runner/renderers.py`: `eval_preset_names` +
      `FlakeManager.list_preset_names` + `build_preset_table` (RendererTable).
- [x] `fxtr_nix_runner/serve.py`: `published_serving_context` — drive the manager
      (entities + namespaces + preset), assemble `create_fxtr_app` +
      `ServiceRegistry` (both `provenanced=True`) + the dispatcher ws service, mount
      the repo store as a deep-fetch source, run hypercorn on a task group, yield a
      `PublishedServer` handle. Graceful shutdown on exit.
- [x] `fxtr_nix serve <flake_dir> --port --rev [--preset]` subcommand in `__main__`
      (lazy-import `serve`), entering the CM and parking until SIGINT/SIGTERM.
- [x] Fixture flake `tests/_workdir_templates/publishing_demo/` — a provenanced
      `fxtr.entities.report` (pure `mkFxtrFnBuilder { provenanced = true; }`), a
      `fxtr.namespaces.demo` tsv export (one bare pin), and a
      `fxtr.workstation.uiPresets.demo` with one renderer
      (`renderers/report/index.tsx` + `fxtr_renderer.nix`).
- [x] Slow nix E2E `tests/test_serve_e2e.py` (backend sweep): seed the namespace
      pin's entity into the repo store; serve via `published_serving_context`; via
      `HttpFxtrReadNamespace` assert the entity round-trips **layered**, the
      namespace pin stays **bare** (bytes resolve via the store),
      `list_namespace_names == ["demo"]` / `list_preset_names == ["demo"]`,
      `build_preset_table` builds the flake renderer into a loadable
      `RendererEntity`, and `/discovery` exposes the `dispatcher` ws service.
      (Pure build + export + esbuild, so it runs on the macOS host backend. The
      dispatcher's ws round-trip is not exercised — see Review notes.)
- [x] Update CODE_OVERVIEW.md (`fxtr2` new module; `fxtr_nix_runner` `serve.py` +
      `renderers.py` + manager methods + the `fxtr_nix serve` CLI; `fxtr_workstation`
      import re-points).

## Review notes (implementation)

Judgment calls + things to double-check during review:

- **No Python ws-client test of the dispatcher round-trip.** The E2E covers the
  renderer build→mint via `build_preset_table` directly and confirms `/discovery`
  lists the `dispatcher` ws service, but it does **not** invoke `resolve` /
  `subscribe` over the wire (there's no Python ws service client; the ws transport
  is covered by the TS `connector-http` integration tests). If we want an
  end-to-end Python wire test, we'd need a small ws client (or a TS test against
  the publishing demo in 7c.2).
- **Generalized the shared `eval_preset_table` / `_build_flake_ref`** (in the
  relocated `renderers.py`) to accept `rev=None` (live-workdir bare ref). The
  `fxtr_workstation` dispatcher always passes a committed rev, so this new path is
  exercised only by the runner — worth a glance that the workstation path is
  unaffected (it is: a `str` rev still produces the same frozen git ref).
- **The E2E renderer's `forType` is a *builtin*, decoupled from the report
  entity's type** — chosen to keep the fixture light (no `mkEntityFromPython`
  venv build for a real type entity). So the E2E proves the renderer/dispatcher
  path but does *not* exercise a content-addressed (layer) typeEntity through
  `build_preset_table`'s validation. 7c.2's real demo will exercise that (its
  renderer's `forType` is the report's actual type). Possibly worth a fixture with
  a layer typeEntity later if we want that path covered in the runner's own tests.
- **Serving scope opened *around* the build span** (`include=[mgr.scope,
  GLOBAL_SCOPE]`) so renderer blobs mint into it during the build — a slight
  reshape from the original plan pseudocode (which closed the span before the
  serving scope). Entities still mount into `mgr.scope`; renderers into the
  serving scope. `GLOBAL_SCOPE` is required for the `RendererEntity` descriptor.
- **Cross-package importers via re-export.** The entity-type relocation keeps
  `fxtr_workstation.dynamic_dispatch`'s `__all__` unchanged (re-exporting from
  `fxtr2`), so the mcp (`fxtr_workstation_agent_plugin`), the sandbox bridge, and
  the `fxtr-ui-dynamic-dispatch-demo` script keep importing the same names. Their
  own envs/CI confirm — verified by identity locally, not by running those suites.
- **`build_preset_table` reuses the relocated helpers with `span.handle`** (no
  rehost env), assuming renderer + typeEntity builds are pure. A typeEntity that
  is itself a remote-pointer FOD would need the rehost env — unusual for type
  entities; not handled.

> A prior pass's full implementation (pre-flake-renderer) is preserved in the jj
> snapshot `wrrtusnz` — individual files were restored from it where unchanged
> rather than rewritten.

## Follow-ups

- **Drop `workstation` from the `fxtr.workstation.uiPresets` attr path** — a clean
  rename everywhere (nix lib + dispatch eval default + demos + docs), folded into
  7c.3 cleanups.
- Possible broader rename / factoring of `fxtr_nix_runner` as the *fxtr project-repo
  manager*.
- **`--no-build`** (serve a previously-built result), **`--watch`** (file-watch →
  rebuild preset + broadcast reload), batched eval-once/build-many — all post-v0.
