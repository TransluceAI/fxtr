# Sandboxed task services (WIP)

**Status: work in progress.** The sandbox side (fxtr_sandboxing, the
workstation spawner, the packaging shapes) is real and demonstrated in
`examples/fxtr_workstation_demo`; the *durable* hookup into
fxtr_experiment (sandboxed bindings, phase 6 —
`plans/phase6_sandboxing/overview.md`) is design-complete but only
partially implemented. This guide describes the target state, with
TODOs where the surface isn't finished. Design canon:
`notes/sandboxed_bindings.md`.

## Why sandbox a service?

Two reasons, and they reinforce each other:

1. **Reproducibility.** A sandboxed service is defined by a nix flake
   attribute at a git commit — a hermetic build recipe. Anyone with the
   repo can spawn bit-for-bit the same environment; the definition is
   data, so it can travel with the workflow (the bindings lockfile) and
   anchor code identity in provenance.
2. **Safety.** Sandboxed code runs with no ambient filesystem or
   network access — capabilities (like "may call the Anthropic API")
   are injected explicitly by the host. That's what makes it safe to
   run agent-written task fns, or experiments cloned from strangers via
   the Behaviors Hub.

Same-process (guide 3) is the right mode while *you* are iterating on
trusted code; sandboxing is the mode for anything agent-written,
anything published, and anything you want pinned.

## Packaging a sandboxed service

A sandboxed service is ordinary Python: a package exposing an
entrypoint that receives a connect config and returns/mounts the
service impl. The repo's nix lib builds it into one of three sandbox
flavors, declared in your workdir/project `flake.nix`:

- **wasm** (`NixWasmServiceDefn` / `mkWasmService`) — Python on WASI.
  Lightest weight; pure-python wheels only (no native extensions — e.g.
  call HTTP APIs with raw httpx instead of vendor SDKs).
- **OCI container** (`NixContainerServiceDefn`) — real Linux CPython in
  podman; native deps fine (the actual `anthropic` SDK, polars, ...).
  Slow first build, cached after.
- **pyodide/deno** (`NixPyodideServiceDefn`) — the middle ground:
  supports PEP 783 binary wheels, ~1.5s spawns, V8-isolate-grade
  isolation.

The worked packaging examples are the workstation demo's
`agent_workdir/packages/` (see its README): `delayed_echo/` for the
split-package shape (host-importable marker package for types +
`sandbox_spawn/` for the sandbox env), `haiku_service_wasm/` vs
`haiku_service_oci/` for the wasm-vs-container tradeoff, and the
network story below.

Two rules that shape the code:

- **Declare everything.** The sandbox forces the flake to state the
  full environment, and the connect contract to state the full input
  surface — which is exactly what makes the service reproducible and
  its behavior auditable.
- **Network is a capability.** Sandboxes have no egress; the host
  registers **authorizer** services (e.g. an `anthropic-authorizer`
  that pins the upstream URL and injects the API key host-side), and
  the sandboxed code opens tunneled connections through the authorizer
  ref it was handed. Neither the agent nor the sandboxed code ever
  sees the key.

## Spawning (the workstation flow, available today)

In a workstation with the sandbox spawner mounted (the experiment demo:
`--enable-sandbox`), spawning is: commit your workdir, then

```python
defn = NixWasmServiceDefn(git_commit=await current_commit(),
                          flake_attr="fxtr.services.my_task_fn").store()
await schedule_invoke("running", spawner, lambda s: s.spawn(defn))
wrapper = await retrieve_result("running")      # exposes .connect(config)
svc = ...  # connect → the typed service ref
```

A spawned task-fn service can then be bound to a workflow handle key
with ordinary `bind_handle` — this works now, but as an **ephemeral**
binding: it lives only as long as the spawned service/mount, and
nothing about it is durable or exported.
**NEEDS CONFIRMATION**: is bind-a-spawned-sandbox-service-ephemerally
actually the demo-supported path today (the experiment demo README's
`--enable-sandbox` section implies an MCP agent can do exactly this)?

## Durable sandboxed bindings (the phase-6 target state)

The end state replaces "spawn, then bind the live ref" with **the
binding is the definition**:

```python
await wf.bind_sandbox(
    key,                                  # the handle key entity id
    defn={"git_commit": ..., "flake_attr": "fxtr.services.my_judge"},
    dependencies={"llm": <selection of a handle cell>},  # pinned at bind
    config={...},                         # machine-local JSON tuning
)
```

- The record is **data owned by the runner** (state db → exported as
  the TOML bindings file); no live object to keep alive, survives
  reopen and travels with export/import.
- Serving is on demand: first use spawns the definition through
  fxtr_sandboxing (against the runner's configured repo,
  `set_sandbox_repo`); processes are cached per binding, with a
  per-binding `shutdown_sandbox` affordance.
- **Dependencies** let a bound service consume other handles: each
  entry is read and its key id pinned at bind time; every connect
  re-checks the pins and pre-spawns the dependency services, delivering
  them in the request. A pin mismatch (someone rewrote the dependency
  cell) faults loudly — re-binding is the explicit "I assert this is
  compatible" act.
- The guest entrypoint receives the same
  `BindingConnectRequest(key, config, handles)` an in-process connector
  does, so code moves between the classes mechanically.

**TODO (tracking `plans/phase6_sandboxing/overview.md`)**: as of this
writing chunk 1 (the request contract) is done; chunks 2–5 (durable
records + `bind_sandbox` on the capability, serving through
fxtr_sandboxing, the bindings-file export/import, and the dashboard
bindings panel) are not. Until then the snippet above does not exist —
use ephemeral bindings. Update this guide chunk by chunk.

**TODO**: once serving lands, add a worked end-to-end example: package
one of the behavior demo's task fns (e.g. the judge) as
`fxtr.services.behavior_judge`, bind it with `bind_sandbox`, export,
re-import on a second machine, `set_sandbox_repo`, re-demand.

## Trust and identity notes

- Bindings — either class — are **not identity-bearing**: swapping what
  serves a key never invalidates results, under the compatibility
  ratchet obligation (guide 3). Anything that *should* invalidate
  consumers belongs in the key entity.
- The handle trust boundary today is *identity, not behavior*: results
  record *which* service key was bound, and you trust the binding.
  Sandboxing strengthens this over time — the deferred plan records the
  nix derivation hash of the actually-running code in the bindings
  file, tying results to code identity.
- **OPEN QUESTION**: what should this guide say about sandboxing the
  *task fns themselves* vs only the leaf services? The architecture
  supports both (task fns are services behind handles like everything
  else), but the ergonomics of a sandboxed task fn (it must speak the
  view-token wire) presumably lag the leaf-service case — confirm
  what's actually supported/tested.
