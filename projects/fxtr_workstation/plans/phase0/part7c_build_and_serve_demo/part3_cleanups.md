# Part 7c.3 — followup cleanups

> Status: scoped. The catch-all cleanups part for 7c, sequenced **after**
> 7c.1/7c.2 (it wants the manager's adapters + the `uiPresets` flake convention
> exercised by the demo first). Two concrete items below, plus deferred followups.
> Optional / skippable — drop items that turn hairy.

## 1. Drop `workstation` from the renderer preset attr path — DONE

> Renamed `fxtr.workstation.uiPresets.<name>` → **`fxtr.uiPresets.<name>`** (clean
> rename, no alias) across the eval default (`renderers.py` `_PRESET_ATTR_PREFIX` +
> `_PRESET_NAMES_APPLY`), all flake declarations (the two demos + the agent-plugin
> `basic_test_demo` + the runner test fixture), the `set_preset` attr-path strings
> (`run_demo_workstation.py`, dispatch + MCP tests, the sandbox-bridge docstring),
> and docs. Attr-path-only (no id changes). Verified by `test_serve_e2e` (host):
> `list_preset_names == ["demo"]` + `build_preset_table` over the renamed fixture.

The `workstation` segment was a misnomer once publishing flakes (no workstation
involved) declare presets too. The dispatcher's `set_preset` takes the attr path
explicitly, so the only hardcoded default was the runner's `_PRESET_ATTR_PREFIX`;
everything else is convention (flake declarations + caller strings + docs).

## 2. Port the MCP's `retrieve_nix_entity` onto the manager — DONE

> Ported `McpHostImpl.retrieve_nix_entity` to delegate to
> `mgr.entities(span).retrieve(name, nix_location=…, into_scope=script_scope,
> copy_into_scope=True)`. Generalized `EntitiesReadAdapter` with an optional
> `nix_location` (default `fxtr.entities`, any attr under `fxtr`) so the MCP's
> caller-supplied location threads through. Deleted the now-dead `nix_util.py`
> (+ `test_nix_util.py`); the friendly no-`flake.nix` pre-check is inlined.
> `.fxtr/` added to the agent-workdir `.gitignore`s (the manager mints a repo
> store there). Verified by `test_in_memory_e2e_retrieve_nix_entity` (podman).
>
> Notes: (a) `FlakeManager` mints the workdir's `.fxtr/` repo store — intended
> (agent workdirs are meant to carry repo stores; the MCP is a normal flake
> consumer), and remote-pointer closures now resolve via it. (b) `build_span`
> runs the repo-store rehost as a concurrent task in an anyio task group, which
> re-wraps any exception crossing its boundary in an `ExceptionGroup`; the MCP
> avoids that by catching the adapter's KeyError/`NixEntityError` *inside* the
> build span (at its source) and raising the mapped `NixEntityBuildFailed` after
> the context managers exit. (c) A missing `layerKey` now maps to
> `NixEntityBuildFailed` (was `LookupError`) — both agent-facing build errors.

### (original spec)


Replace the workstation MCP's hand-rolled `retrieve_nix_entity` implementation
(eval-metadata → build → materialize → open-reader, in
`fxtr_workstation_mcp/host_impl.py` + `nix_util.py`) with a thin call into the
`fxtr_nix_runner` **manager**'s entities adapter (`mgr.entities(span).retrieve`,
the same path 7c.1's serve uses).

**Payoff:** the MCP gains **transparent remote-pointer resolution** for free —
today it would error or mishandle an entity whose layer is a remote pointer; with
the manager, the adapter's reader carries the repo store as `remote=`, so a
remote-pointer closure resolves on read. The MCP also stops duplicating the
build/materialize dance the manager already owns.

### Current state (orient when implementing)

- **MCP side**: `projects/fxtr_workstation_agent_plugin/.../fxtr_workstation_mcp/`
  — `host_impl.py` (the `retrieve_nix_entity` host method) + `nix_util.py` (the
  hand-rolled eval/build/materialize/open-reader). Read these for the exact
  surface + the friendly `NoFlakeNixError` pre-check + error conventions to
  preserve.
- **Manager side**: `projects/fxtr_nix_runner/src/fxtr_nix_runner/manager.py`
  (`FlakeManager` async CM, `build_span`, `entities(span).retrieve(name)`) — the
  adapter the MCP should delegate to. `retrieve` returns a `FxtrID` / `EntityRef`,
  mounting into the manager scope; remote-pointer closures resolve via the reader's
  `remote=` repo store.

### Approach (sketch — confirm against the code)

- Swap the MCP's hand-rolled retrieval for `async with FlakeManager(repo, rev=…)
  as mgr: async with mgr.build_span() as span: await mgr.entities(span).retrieve(name)`,
  threading the result back through the MCP's existing result-shaping.
- **Keep** the MCP's `NoFlakeNixError` pre-check (friendly "this isn't a flake"
  message) and its error-surface conventions — only the build/retrieve internals
  change.
- **Provenance**: a provenanced layer now reads back as a *layered* `FxtrID`.
  Confirm the MCP's result shaping tolerates a layered id (it should — it surfaces
  an id_string / loads the entity; provenance rides along). Decide whether the MCP
  exposes provenance at all or just serves the data id (likely the latter for now —
  out of scope to surface provenance through the MCP wire).

### Implementation steps

- [ ] Read `host_impl.py` + `nix_util.py`; map the current retrieval flow + error
      surface.
- [ ] Replace the eval/build/materialize/open-reader dance with a `FlakeManager` +
      entities-adapter call; preserve `NoFlakeNixError` + error conventions.
- [ ] Confirm layered-id tolerance (provenanced layers) in the MCP result shaping.
- [ ] Tests: an MCP-level retrieve against a fixture flake (reuse 7c's fixture if
      handy), incl. a remote-pointer entity that the old path couldn't resolve.
- [ ] Update `fxtr_workstation_mcp/CODE_OVERVIEW.md` + remove the now-dead
      hand-rolled helpers.

## Deferred followups (tracked, not in this part)

- **Serve the viewer bundle by default, built by nix.** 7c.2 ships the `fxtr_nix
  serve --web-dist <dir>` static mount, where the operator builds
  `@fxtr-ui/published-app` (`pnpm build`) and points serve at its `dist/`. A nicer
  variant removes the manual build + flag: have the **root `fxtr` flake declare a
  derivation** that builds the viewer's static assets, so `fxtr_nix serve <flake>`
  resolves + serves it **by default** (à la the workstation server's
  `debug_ui_dist_path`), with `--web-dist` as an override + a `--no-web` escape.
  **Deferred — minimal extra value over `--web-dist` today, and the hard part is
  real design**: unlike `lib.reportBuilderCli` (a deliberately standalone package
  with its own lockfile, exactly what `pnpm.fetchDeps` wants), the viewer is a pnpm
  **workspace member** with `workspace:*` deps on a dozen `@fxtr-ui/*` packages, so
  building it pure-in-nix means feeding `fetchDeps` the whole-repo workspace closure
  (or carving a self-contained sub-closure). Pick this up as its own scoped effort.
- **Rename / refactor `fxtr_nix_runner`.** It has grown into the central manager of
  fxtr project state (repo store + entities/namespaces/renderers + serving), not
  just a nix runner — a rename + manager-tier factoring is a plausible cleanup.
  Deliberately **not** folded into 7c.3: it's a cross-cutting rename with its own
  churn, to pick up as a standalone effort.

## Out of scope

- Surfacing provenance through the MCP wire (the MCP serves the data id; the
  provenance-on-the-wire story is the HTTP stack's, 7a.6/7a.7).
