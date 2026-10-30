# UI presets + the nix/git dynamic dispatcher

How an agent's flake declares dispatcher renderers and pushes them to a
running workstation dispatcher.

> **Revised (2026-05-30):** a preset is no longer a serialized
> `DispatcherPreset` **entity** built by `mkDispatcherPreset` and loaded
> by name. It's a **plain nix attrset** of renderer recipes, keyed by
> type id_string, referenced by `(git_commit, flake_attr, flake_path?)`.
> The dispatcher builds the renderer ESM derivations itself and wraps
> the bytes in a `RendererEntity` *at serve time*. `mkDispatcherPreset`
> and `mkRendererEntity` (and the `write_renderer_entity` /
> `write_dispatcher_preset` writers) are gone; `esbuildRendererEntity`
> → `esbuildRenderer` (returns an ESM derivation, not an entity).
> `LiveConfigurableDispatcherProto` → `NixDispatcherProto`.

> _NOTE 2026-06-02: the per-subtype `dispatchKeys` field shown later in this
> note was renamed to `forDispatchKeys` (to parallel `forType`); the example
> keeps the original name as a record of the design at the time._

## Mental model

Today's `BasicDynamicDispatchImpl` discovers renderers by walking
on-disk `renderer_dirs`, runs `npx esbuild` per renderer, and self-
manages its `type_id_string → RendererResolution` table with mtime
polling.

In the new flow:

- The agent's flake declares one or more **presets** under
  `fxtr.workstation.uiPresets.<name>` — each a **plain attrset** (CODE,
  not an entity) describing, per type, which renderer covers which
  dispatch key.
- Renderer modules are produced by **ESM derivations** (esbuild inside
  a derivation). The preset attr points at those derivations; it does
  **not** wrap them in entities.
- The dispatcher impl (`NixConfigurableDispatchImpl`) is configured with
  one **git repo** and implements `NixDispatcherProto` (below). The
  agent calls `set_preset(git_commit, flake_attr, flake_path?)`; the
  dispatcher resolves the attr via a git ref, builds the renderer ESMs,
  wraps each in a `RendererEntity` it creates in the workstation store,
  swaps its table, and broadcasts a reload.
- The dispatcher optionally auto-loads an initial `(commit, attr)`
  configured at construction, so restarts come up with a UI.

## Preset attr shape (plain nix)

Keyed by the type's **id_string** as the attribute name. The agent is
responsible for finding and pinning that id_string (see "Getting a
type's id_string" below). Each entry also attaches the type's entity
(via `mkEntityFromPython`) purely for introspection.

```nix
fxtr.workstation.uiPresets.my_preset = {
  "<type-id-string>" = {
    typeEntity = myType;          # mkEntityFromPython attrset — introspection only
    byDispatchKey = {
      "<dispatch-key-uuid>" = {
        subtypeKey = "default";   # module[subtypeKey][keyUuid] in the frontend
        renderer   = notesRenderer;  # an ESM derivation (esbuildRenderer output)
      };
    };
  };
};
```

Why this shape:

- **Type keyed by id_string, as the attr name.** This is the
  dispatcher's table key. Because it's the attribute *name*, the
  dispatcher gets the whole `{type_id_string → routing}` map from a
  plain `nix eval` — **no builds needed to populate the table keys**.
  (The old design keyed by a full `FxtrID` for the serialized preset's
  introspectability; with a plain attrset that rationale is gone, and
  the id_string is all the dispatcher needs to key/look up.)
- **`typeEntity` attached for introspection.** If either side wants to
  answer "what entity is this type?", it can build `typeEntity` and
  read it. Not needed to populate the table; it's a debugging
  affordance.
- **`renderer` is an ESM derivation, not an entity.** The dispatcher
  builds it and wraps the bytes itself.
- **`byDispatchKey` keyed by dispatch-key UUID (string).** Mirrors
  `BasicDynamicDispatchImpl`'s lookup-table shape; `subtypeKey` +
  `renderer` match `RendererResolution`'s wire shape.

## Getting a type's id_string

A FxtrID is content-addressed — you can't synthesize the id_string
without running Python over the type definition. The agent obtains it
the same way the existing demos already document: in a `run_script`,
`print(MyType.fxtr_type.id_string())` (or `retrieve_nix_entity` the type
entity, then read `.id_string()`), then pastes that string as the
preset's type-keyed attribute name.

> **Revised (2026-06-01):** an earlier draft proposed a nix-native
> `entityIdString { entity }` helper for this. It was **dropped** — the
> Python id_string print is simpler and the agent already does it. The
> `typeEntity` slot in the preset stays (it's what eager validation
> builds; see below).

## Protocol split

The workstation-facing `WorkstationDispatcherProto` (`resolve` +
`subscribe_to_changes`) is **unchanged**. The frontend needs no work.

The extended protocol is renamed to be honest about being nix/git-
specific:

```python
@service_protocol(...)
class NixDispatcherProto(WorkstationDispatcherProto):
    async def set_preset(
        self, git_commit: str, flake_attr: str, flake_path: str | None = None
    ) -> None: ...
```

`set_preset` takes a **git reference**, not a name or an entity ref —
the impl resolves the attr against its configured repo. Only the nix/git
impl exposes `NixDispatcherProto`; clients that only consume resolutions
target the base protocol.

## New impl: `NixConfigurableDispatchImpl`

Lives alongside the existing `basic_dynamic_dispatch/` directory
(layout cosmetic). Implements `NixDispatcherProto`:

- **Construction.** `NixConfigurableDispatchImpl(scope, repo, impl_tg,
  *, initial_commit=None, initial_attr=None, initial_flake_path=None,
  ...)`. `repo` (the git repo the dispatcher resolves refs against) is
  **required**; the launcher (`run_demo_workstation.py` / equivalent)
  wires it in — locally the same repo as the agent's workdir. The
  initial `(commit, attr)` is **optional**.
  - **Repo + multi-agent** (see `12_nix_drv_reference.md` "Trust model
    & topology"): the dispatcher resolves preset refs against its one
    configured `repo`; multiple agents are fine if they push to a
    shared bare repo. Because the browser UI is workstation-wide,
    `set_preset` is workstation-global — last write wins (replace-
    semantics below), so "whose preset is active" is a UI-ownership
    question, not a repo limitation.
- **Internal state:** `_table: dict[str, dict[str, RendererResolution]]`
  (`{type_id_string: {key_uuid: RendererResolution}}`). Empty at
  construction. Plus a cache of built `RendererEntity` refs keyed by
  (commit, renderer-attr) so repeated builds are cheap.
- **Startup auto-load.** If `initial_commit` + `initial_attr` are
  provided, the initialization step applies them the same way
  `set_preset` does, so restarts pick up a UI. If absent, the impl
  starts with an empty table (every dispatch resolves to
  `NoMatchFallback` until the agent calls `set_preset`). **Auto-load
  never crashes the workstation:** any failure (missing/stale ref,
  eval/build error, malformed preset) logs a warning and comes up with
  an empty table — the agent recovers by calling `set_preset`, and the
  empty-table debug surface (below) makes the unloaded state visible.
  (An agent-invoked `set_preset` is different — it *raises* so the
  agent sees the error; see `11_failure_and_ux.md`.)
  - **Empty-table visibility** (open-concerns #5): the dispatcher's
    debug view surfaces "currently-loaded ref: none / `(commit, attr)`"
    so an agent who never pushed a preset can diagnose why everything
    renders as `NoMatchFallback`.
- **`set_preset(git_commit, flake_attr, flake_path)`:**
  1. Form the git flake ref
     `git+file://<repo>?rev=<git_commit>&dir=<flake_path>#<flake_attr>`.
     Eval/build run **`--no-update-lock-file`** so a stale/missing committed
     `flake.lock` errors rather than silently re-locking (Area 12).
  2. `nix eval --json` the preset attr → the `{type_id_string →
     {key_uuid → {subtypeKey, renderer-attr-path}}}` routing table.
     **No renderer builds yet** — the table keys come from eval.
  3. For each renderer-attr in the table, `nix build` its ESM
     derivation (eagerly here for v0 simplicity; lazy-on-first-resolve
     is a valid alternative), read the `.mjs` bytes, wrap them in a
     `RendererEntity(module=bytes)` created in the **workstation store**,
     and record the resulting `EntityRef[RendererEntity]` in a
     `RendererResolution`. Cache by (commit, renderer-attr).
  4. Rebuild `_table` from scratch and swap it atomically; fire the
     existing `trigger_reload()` broadcast.

  On failure (git fetch / eval / build / malformed attr) raises with
  the relevant stderr; surfaces to the agent as `invoke_error`. Trust
  nix's eval/build cache — repeat `set_preset` of the same commit is
  cheap.
- **`resolve(type_ref, key_uuid)`** is a **pure lookup**:
  `_table.get(type_ref.entity_id.id_string, {}).get(key_uuid)`; misses
  return `None`. No build, no validation (done eagerly in `set_preset`).
  (Keying by id_string is sufficient — `resolve` only compares ids.)

**Key ↔ `typeEntity` validation (eager, at `set_preset`).** The preset's
type key is a *hand-pasted* id_string (printed from Python —
`MyType.fxtr_type.id_string()`), so it can drift from the attached
`typeEntity` if the agent edits a type and forgets to re-paste —
silently keying a stale id and rendering as `NoMatchFallback` with no
explanation. So `set_preset`, alongside the eager renderer builds, also
builds each entry's `typeEntity` and asserts its id == the entry's key;
on mismatch it raises a **"renderer misconfigured"** error (rejecting
the whole preset, table not swapped) → the agent sees an `invoke_error`
at once.

> **Revised (2026-06-01):** an earlier draft did this validation
> *request-time / lazy* (on `resolve`). That was dropped: `resolve` only
> validates on a **hit**, and on a hit the key already equals
> `type_ref.id_string()` — so a *stale* key makes `resolve` **miss** →
> `NoMatchFallback`, and the mis-keyed entry is never validated. Lazy
> validation thus can't catch the very failure it exists for. Eager
> validation (iterate all entries at `set_preset`) catches both the
> stale-key and wrong-`typeEntity` cases, where the agent can fix them.
> It's also simpler (no resolve-path build/cache) and consistent with
> eager renderer builds. If renderer builds ever go lazy (a deferred
> optimization), revisit validation then.
- **`subscribe_to_changes(listener)`** reuses the existing listener-
  worker pattern from `BasicDynamicDispatchImpl` verbatim.

**Wrapping ESM bytes into a `RendererEntity` at serve time** is what
keeps `WorkstationDispatcherProto` unchanged: the frontend still loads
opaque ESM bytes via its normal entity-load path; the dispatcher just
mints those entities on the fly from freshly-built ESMs instead of
reading them from a flake-built layer. `RendererEntity` keeps its
existing `{module: bytes}` shape.

**Replace-not-merge semantics.** `set_preset` always rebuilds the table
from the new preset attr. Agents who want a layered effect compose a
single preset attr in their flake.

**Nix wrapper module.** The dispatcher's git-flake-ref eval/build calls
live in a workstation-side wrapper (analogous to
`fxtr_workstation_mcp/nix.py` and `fxtr_sandboxing/nix.py`). Three
parallel wrappers → a shared lib someday (follow-up).

## Helpers (`lib.fxtr.*`)

- **`mkEntityFromPython { venv, fromPython }`** — materialize a Python-
  resolved entity (a type's `FxtrID`) into a layer. The agent supplies
  `venv` (which carries fxtr2). Used for the preset's `typeEntity` slots.
- **`esbuildRenderer { src, externals ? lib.fxtr.workstationExternals }`**
  — run esbuild on `src/index.tsx` with the externals list and produce
  an **ESM derivation** (a `.mjs`). Returns a derivation, *not* an
  entity. Flake authors who want a different bundler pass their own ESM
  derivation directly into the preset attr's `renderer` slot.

No `mkRendererEntity` / `mkDispatcherPreset` — the dispatcher does the
entity-wrapping and table-assembly at runtime, so there's nothing to
pre-write. A preset is hand-written nix (or assembled by `mkUiPreset`
below, or the author's own nix helpers); there's no fxtr-side writer for it.

### Preset assembly via `mkUiPreset` (the transpose helper)

> **Added (2026-06-01):** restores the per-renderer-declaration pattern the
> pre-nix `demo_dispatch.py` had (each renderer carried an `fxtr_renderer.json`
> declaring what it supports; `_build_routing_table` transposed those into the
> query table + detected duplicate `(type, key)` claims). The Stage-5 preset
> attr is the *assembled* form, which lost that. See
> `stage5post1_ui_preset_transpose.md`.

`lib.fxtr.mkUiPreset` is a **pure structural transpose** — the nix analog of
`_build_routing_table`. It takes a **name-keyed attrset of per-renderer
declarations** and folds them into exactly the type-id-keyed table above (so
`eval_preset_table` / `_PRESET_APPLY` / the dispatcher are unchanged), throwing
on a duplicate `(type-id, dispatch-key)` claim. It builds and imports nothing —
the `renderer` (an `esbuildRenderer` output) and the type's `entity` (an
`mkEntityFromPython` attr) pass through untouched.

```nix
let
  # The type defined once: hand-pasted id_string + its entity, bundled so
  # every shard references one value (printed from Python, decision X).
  demoDataType = { idString = "dlt7…piuhq"; entity = demoDataTypeEntity; };
in
lib.fxtr.mkUiPreset {
  demo_data = {                                   # entry name (labels collisions)
    renderer = demoDataRenderer;                  # one ESM drv per entry
    subtypes."demo_data" = {                       # map key = subtypeKey
      forType      = demoDataType;
      dispatchKeys = [ "21b2…" "b3c4…" … ];        # user-owned uuid literals
    };
  };
}
```

- **`forType` bundles `{ idString, entity }`** so the type is defined once and
  referenced from every shard; the type-level `typeEntity` in the output is set
  by the first row naming the type (first-wins — a non-issue when shards share
  the value), and the dispatcher's eager key↔`typeEntity` validation backstops a
  stale paste.
- **Multiple subtypes per entry** (the `subtypes` map, like
  `fxtr_renderer.json`'s top level); the map key *is* the `subtypeKey`. One
  compiled `renderer` backs all of an entry's subtypes, and one entry may
  declare subtypes for different types.
- **Sharding** one type across entries works as long as the `(type, uuid)`
  pairs don't overlap (same rule as the Python version).
- **Conflicts `throw`** naming both entries; the throw fires when `set_preset`'s
  `--apply` forces the attr → `NixPresetEvalError` → `invoke_error`.

The hand-written table form still works (it's the same shape `mkUiPreset`
emits); `mkUiPreset` is purely additive ergonomics for multi-renderer presets.

## Submission flow (agent side)

```python
dispatcher = lookup_entity(...).resolve()
schedule_invoke("r", dispatcher,
    lambda d: d.set_preset(git_commit="<hash>", flake_attr="fxtr.workstation.uiPresets.my_preset"))
```

The agent commits the flake (pushes, for a remote dispatcher), then
hands the dispatcher a git ref. No `retrieve_nix_entity` round-trip —
presets aren't entities.

## What's automatic vs. explicit

- **Automatic.** If the launcher configured an initial `(commit, attr)`,
  the dispatcher auto-loads it on startup.
- **Explicit.** Switching presets is always agent-driven via
  `set_preset(...)`. No per-script auto-detection.

Not in v0: operator-driven preset selection through the frontend
(Q6.A); lazy-on-resolve renderer builds (v0 builds eagerly on
`set_preset`).

## Deprecation of the old impl

> **Revised (2026-06-01):** `BasicDynamicDispatchImpl` (`compile.py`,
> `service_impl.py`, `mount_basic_dynamic_dispatch`) is **not physically
> moved** to a `deprecated/` dir in Stage 5 — that would churn the
> several live importers (`fxtr_workstation_demo/demo_dispatch.py`, the
> `frontend_lib` dynamic-dispatch demos) for no v0 benefit. Its
> docstrings just gain a "no longer the suggested impl — prefer
> `NixConfigurableDispatchImpl`" note.

Its protocol surface (`WorkstationDispatcherProto`) is unchanged, so
anything still pointed at it keeps working. The dynamic-dispatch demos
migrate to `NixConfigurableDispatchImpl` + a flake declaring the same
renderers as a preset (per Area 09) in Stage 6.

## Open questions

- Q6.A: operator-driven preset selection through the frontend. Out of
  scope for v0; follow-up.
- Q6.B (resolved): hand-rolled preset attrs are fine — just expose the
  type-id-keyed routing shape with buildable `renderer` derivations.
- Q6.C (resolved): subpackage layout is
  `dynamic_dispatch/nix_configurable_dispatch/` (`nix.py` +
  `service_impl.py`), sibling to `basic_dynamic_dispatch/`. See
  `stage5_ui_presets.md` "Module layout".
- Q6.D (resolved): **eager** renderer builds on `set_preset` for v0
  (simple; a cold cache just makes `set_preset` slower). Lazy-on-resolve
  (build + cache the `RendererEntity` on first `resolve` miss) is a
  clean later optimization.
- Q6.E (resolved / dropped): the `entityIdString` helper was dropped —
  the agent prints the id_string from Python in a `run_script`. See
  "Getting a type's id_string" above.
- Q6.F (new): does the workstation store need any GC consideration for
  the `RendererEntity`s the dispatcher mints at serve time? Probably
  not for v0 (small, few). Note if it bloats.
