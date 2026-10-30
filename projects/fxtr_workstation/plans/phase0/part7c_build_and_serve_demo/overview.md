# Part 7c — build-and-serve demo (overview)

> Status: scoped, ready to implement. Its dependencies (7b provenanced layers +
> nix builder flags, 7a the HTTP provenance wire + connector) are all landed, so
> this is example/assembly work, not new mechanism design.

## Goal

A single end-to-end demo that **builds a published report as a nix flake** and
**serves it over the stateless HTTP stack**, with **provenance preserved all the
way through** — a node that carries provenance is threaded through nix build
logic, served, and has its provenance appear on the client exactly as everywhere
else in the system. It lives under `examples/fxtr_publishing_demo/` — the
publishing-flavored sibling of `examples/fxtr_workstation_demo/`.

## How the pieces fit

- **Build side (7b).** A nix flake builds layered fxtr entities; the layered file
  store carries provenance (provenanced layers); `mkFxtrFnBuilder` /
  `mkFxtrScriptBuilder` take `provenanced` + `provenanceInfo` so a build step
  mints a provenance context and its outputs are layered ids. The
  `fxtr_nix_runner` **manager** drives eval/build/materialize and exposes read
  adapters (entities + namespaces).
- **Serve side (7a).** The stateless HTTP backend (`create_fxtr_app` +
  `ServiceRegistry` + the ws dispatch endpoint) serves entities/services; with
  `provenanced=True` it emits layered `(data, prov)` ids on the wire, and the
  `@fxtr-ui/connector-http` + PanelViewer frontend consume them (a layered entity
  surfaces `provenance_parent`; loading it yields layered children).
- **This part (7c).** A **general** serving capability in `fxtr_nix_runner`
  (`fxtr_nix serve`) that drives the manager and serves a flake's entities +
  namespaces **and a flake-built dynamic-dispatch service** (renderers built from
  `fxtr.workstation.uiPresets.<name>`) over the HTTP backend — nothing
  demo-specific. The demo (7c.2) is then just a flake (entities/namespaces +
  `uiPresets`) + a web frontend + an end-to-end test.

## What's in this directory

- `overview.md` — this file.
- `part1_serving_script.md` — the general `fxtr_nix_runner` serving capability:
  `serve.py` (`published_serving_context`) + the `fxtr_nix serve` CLI, serving a
  flake's entities + namespaces + a **flake-built** dynamic-dispatch service.
  Includes relocating the dynamic-dispatch entity types down to `fxtr2` and the
  preset nix helpers down to `fxtr_nix_runner` (so the serve is flake-agnostic).
- `part2_example_and_e2e.md` — the `examples/fxtr_publishing_demo/`: a flake (a
  small `Note → Stats → SubReport → RootReport` graph, provenanced; `uiPresets`
  renderers — re-exported `@fxtr-ui/reports` + custom `Note`/`Stats`) + scaffold +
  seed + the headless E2E. The demo ships **no frontend**: it's viewed through a
  new demo-agnostic **`@fxtr-ui/published-app`** served by `fxtr_nix serve`
  (`--web-dist`). No custom backend.
- `part3_cleanups.md` — optional/skippable followup cleanups: dropping
  `workstation` from the `fxtr.workstation.uiPresets` attr path (a clean rename
  everywhere) + porting the workstation MCP's `retrieve_nix_entity` onto the
  manager's entities adapter. (The `fxtr_nix_runner` rename is a deferred followup,
  not folded in.)

## Sequencing

```
7c.1 general serve (entities + namespaces + flake renderers)
   →  7c.2 demo flake + frontend + E2E
   →  7c.3 followup cleanups (uiPresets rename + MCP port) (optional)
```

7c.1 stands up the general serving capability; 7c.2 is the example flake + client
that run against it; 7c.3 is the followup cleanups (the `uiPresets` rename + the
MCP `retrieve_nix_entity` port), sequenced last (it wants the adapters + the
`uiPresets` convention exercised by the demo first).
