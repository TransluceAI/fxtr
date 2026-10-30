# Stage 6 pre-1 — builtin entities + `lib.fxtr.entities`

A `lib.fxtr` prerequisite for the Stage 6 demo migration. Two
capabilities, both motivated by the big demo's `builtin/` renderer
(the agent-panel + log-event + report + `!basic_type` slots that the
nix dispatcher must serve so the operator's panel renders at all):

1. **Builtin entities** — a second entity-attr shape, `{ type =
   "builtin"; idString = "<id>"; }`, that the preset path understands
   without building a layer. Needed for the `!basic_type` fallback key
   (a primitive `FxtrID` whose id_string *is* itself, so there's no
   content to pin in a layer) and useful for any other primitive type.
2. **`lib.fxtr.entities.*`** — ready-made `forType`-shaped values for
   the type ids that live in *library* packages
   (`fxtr_workstation_mcp`, `fxtr_report_builder`), so the demo flake
   imports them instead of standing up its own venv over those
   packages.

Neither touches the demo; both are pure `lib.fxtr` (+ a small
dispatcher / `_PRESET_APPLY` change) additions. This lands before
Stage 6.

> Why a pre-stage and not "no new plumbing": Stage 6's plan originally
> said the demo migration introduces no new library components. The
> `builtin/` renderer breaks that — `!basic_type` can't be a layer
> entity, and `mkUiPreset`/the dispatcher validate every key against a
> built `typeEntity`. Rather than bend the demo, we add the small,
> general capability here. See `06_ui_presets.md` (eager validation)
> and the demo's `agent_workdir/renderers/builtin/fxtr_renderer.json`
> (the `"!basic_type"` `for_type_id`).

## Background: the entity-attr shape today

`fxtr.entities.<name>` / a preset's `forType.entity` is the
three-field layer entity:

```nix
{ type = "fxtr_layer_entity"; layer = <drv>; layerKey = "<key>"; }
```

The dispatcher's `set_preset` (eager validation) reads the built
`layer` via `LayeredFileStoreReader`, resolves `layerKey` to an
`FxtrID`, and asserts its `id_string()` equals the preset's type key
(`nix_configurable_dispatch/service_impl.py`, `_validate_*`). The
`_PRESET_APPLY` reshape in `nix_configurable_dispatch/nix.py` emits
`typeEntityLayerDrv` + `typeEntityLayerKey` per entry, and the impl
batches every `typeEntityLayerDrv` into the one `build_drv_outputs`
call.

## Part 1 — builtin entities

### The shape

```nix
{ type = "builtin"; idString = "!basic_type"; }
```

A primitive `FxtrID` (the `!`/`.`/`p`/`u`-tag family) is its own
content-addressed id, so the id_string is the *whole* entity — nothing
to pin in a layer. The builtin shape carries the id_string directly.

### `lib.fxtr.builtinEntity` (new helper)

A trivial constructor so authors don't hand-write the attr:

```nix
lib.fxtr.builtinEntity { idString = "!basic_type"; }
# → { type = "builtin"; idString = "!basic_type"; }
```

Added in `nix/lib/default.nix` (inline or a one-line file). Used by
`lib.fxtr.entities.basicType` below and available to any flake author.

### `_PRESET_APPLY` discriminates builtin vs layer

`nix_configurable_dispatch/nix.py`'s `_PRESET_APPLY` inspects
`r.typeEntity.type` and emits one of two entry shapes:

```nix
preset: builtins.mapAttrs (idStr: r:
  let te = r.typeEntity; in
  { byDispatchKey = builtins.mapAttrs (uuid: e: {
      inherit (e) subtypeKey; rendererDrv = e.renderer.drvPath;
    }) r.byDispatchKey;
  } // (if te.type or "" == "builtin"
        then { builtin = true; idString = te.idString; }
        else { typeEntityLayerDrv = te.layer.drvPath;
               typeEntityLayerKey = te.layerKey; }))
  preset
```

So a builtin entry comes back as `{ builtin = true, idString,
byDispatchKey }` — **no drv**, nothing to realize for it.

### Dispatcher `set_preset` honours the discriminator

In `nix_configurable_dispatch/service_impl.py`:

- **Collecting `type_entity_drvs`** for the batched build skips entries
  that carry `"builtin"` (no `typeEntityLayerDrv` to build).
- **Validation** branches per entry: a layer entry runs the existing
  `LayeredFileStoreReader` id-check; a builtin entry just asserts
  `entry["idString"] == <preset key>` (cheap, no build). Mismatch →
  the same `IRException(category="RendererMisconfigured")`.
- Everything downstream (`byDispatchKey`, renderer builds, table swap)
  is unchanged — the key is still the attr name (the id_string), and
  `resolve` is still a pure `_table[id_string][uuid]` lookup, so a
  primitive type ref whose `id_string()` is `"!basic_type"` resolves
  exactly like any other.

`mkUiPreset` needs **no change**: it already passes `forType.entity`
through to `typeEntity` untouched, and a builtin `forType` is just
`{ idString = "!basic_type"; entity = lib.fxtr.builtinEntity {...}; }`.

### Part 1b (optional, for consistency) — `retrieve_nix_entity` builtins

So a script can also pull a builtin type as a bare `FxtrID` without a
build. `fxtr.entities.<name> = lib.fxtr.builtinEntity { idString; }`
resolves in the MCP's `retrieve_nix_entity` path
(`host_impl.retrieve_nix_entity` + `nix_util.eval_entity_metadata`):

- `eval_entity_metadata` already evals the entity attr's metadata
  fields. Teach it to recognize `type == "builtin"` and return the
  `idString` instead of expecting `layer`/`layerKey`.
- `retrieve_nix_entity`, on a builtin, **skips `build_attr`** and
  constructs the primitive `FxtrID` from the id_string directly
  (parse via the existing `FxtrID` id-string constructor), bridges it
  into `script_scope` as usual.

This half is small and self-contained; include it if it falls out
cleanly, otherwise it can trail (the demo only needs Part 1's preset
side). Mark it done/deferred in the checklist accordingly.

## Part 2 — `lib.fxtr.entities.*`

Ready-made `forType` values for the library/builtin types the demo's
`builtin/` renderer routes. Each is `{ idString = "<literal>"; entity
= <mkEntityFromPython | builtinEntity>; }` — exactly what `mkUiPreset`'s
`forType` slot wants — so the demo flake writes
`forType = fxtr.lib.fxtr.entities.rootReport;` and builds nothing
itself.

### Where the type ids come from

The id_strings are **content-stable literals** (a type's id derives
from its `define_type(NS_UUID, label)`), and we already have them: the
demo's `builtin/fxtr_renderer.json` lists every one. We lift those
literals into `lib.fxtr.entities`, and the dispatcher's eager
validation (Part 1) builds each `entity` and confirms the literal still
matches — so drift is caught, not silently mis-keyed.

> **Revised (implemented 2026-06-02):** the venv design below (depend on the
> heavy `fxtr_workstation_mcp` + `fxtr_report_builder` closures) was
> **superseded by a stub package** — `nix/fxtr_lib_type_ids/`, which
> re-declares each type id via `define_type(SAME_NS, label)` and depends on
> **fxtr2 only**. FxtrIDs are content-addressed, so the re-declaration is
> byte-identical to the canonical type, but the venv stays light (no
> mcp/report closure under uv2nix — which had hit a `cbrrr` build-hint wall
> and would have pulled the ~50 MB `pyeryx` wheel + fxtr_sandboxing). The
> stub's `(NS, label)` pairs were verified to reproduce all 8 baked literals.
> The text below is kept for the rationale; the heavy-venv mechanics are
> obsolete.

### The library venv

`lib.fxtr.entities` needs a venv carrying `fxtr2` + the two library
packages so `mkEntityFromPython` can import the type objects:

- A small uv workspace under `nix/lib/` (e.g.
  `nix/lib/builtin_entities_venv/pyproject.toml`) that depends on
  `fxtr_workstation_mcp` and `fxtr_report_builder` (relative path
  sources within the monorepo flake).
- `lib.fxtr` builds it once via the internal `mkVenv` (this is the
  fxtr-flake's *own* lib-internal venv, the "no `venv` arg" provisioning
  path — `overview.md` "Two fxtr2-provisioning paths").

**Host-importability — verified (2026-06-01).** `mkEntityFromPython`'s
writer does a literal `importlib.import_module(...)` in a plain venv, so
the type module must import with only `fxtr2` + the package present (no
sandbox runtime). Checked: `fxtr_report_builder.entities` imports only
`dataclasses`/`uuid`/`fxtr2.*`; `fxtr_workstation_mcp.agent_log_types`
has no `_fxtr_mcp_builtin` / `fxtr_ws_mcp_sandbox_bridge` import; and
`fxtr_workstation_mcp` is the MCP *host* package anyway. So no
sandbox-bridge import blocks the build. (Worth re-confirming for the
exact module that owns the `agent_mcp_running_session` type once its
constant is located.)

### Exposing the type ids from Python

`mkEntityFromPython`'s `fromPython` needs a module-level object that is
the type-id `FxtrID`. Add `*_TYPE_ID` constants (or `Cls.fxtr_type`
references) where missing, in:

- `fxtr_workstation_mcp.agent_log_types` — the log-event types
  (`log_script_execution` / `log_invoke_result` / `log_invoke_error` /
  `log_callback_notification`) + `WorkstationServicePlaceholder`.
- the module owning the `agent_mcp_running_session` type
  (`agent_mcp_proto` / `agent_log_types` — wherever its
  `define_type` lives).
- `fxtr_report_builder.entities` — `RootReport` / `SubReport`.

The exact constant names are an implementation detail; the comment in
`builtin/fxtr_renderer.json` enumerates the source `define_type`
declarations.

### The attrset

```nix
lib.fxtr.entities = {
  agentMcpRunningSession      = { idString = "db7cf…"; entity = mkEntityFromPython { venv = builtinVenv; fromPython = "…"; layerKey = "…"; }; };
  logScriptExecution          = { idString = "dzrgd…"; entity = …; };
  logInvokeResult             = { idString = "djhhw…"; entity = …; };
  logInvokeError              = { idString = "d5pv2…"; entity = …; };
  logCallbackNotification     = { idString = "dvguv…"; entity = …; };
  workstationServicePlaceholder = { idString = "duzfd…"; entity = …; };
  rootReport                  = { idString = "du7ae…"; entity = …; };
  subReport                   = { idString = "d4nqy…"; entity = …; };
  basicType                   = { idString = "!basic_type"; entity = builtinEntity { idString = "!basic_type"; }; };
};
```

(Literals copied verbatim from `builtin/fxtr_renderer.json`.)

## Implementation steps

- [x] **Part 1a — builtin preset path.** Added `lib.fxtr.builtinEntity`
  (`nix/lib/builtinEntity.nix` + `default.nix`). Updated `_PRESET_APPLY`
  (`nix_configurable_dispatch/nix.py`) to discriminate `type == "builtin"`
  (verified via synthetic `nix eval`). Updated `set_preset`
  (`service_impl.py`) to skip the build for builtin entries +
  `_validate_builtin` (`idString == key`).
- [x] Unit tests: `test_set_preset_builtin_entity_skips_layer_build`
  (resolves + reloads, asserts the typeEntity drv is never requested from
  the build) + `test_set_preset_builtin_idstring_mismatch_raises`
  (`RendererMisconfigured`, whole-preset reject) in
  `tests/test_nix_configurable_dispatch.py`. **All 7 pass** (builder-free).
- [x] Update CODE_OVERVIEW.md (`fxtr_workstation` — builtin entry shape in
  the `nix_configurable_dispatch` `nix.py` + `set_preset` notes).
- [x] **Part 1b — `retrieve_nix_entity` builtins.** `eval_entity_metadata`'s
  `--apply` now reads `idString` for a `type == "builtin"` entity;
  `eval_entity_metadata` returns a `LayerEntityMetadata` /
  `BuiltinEntityMetadata` **union** (split per review — no catch-all optional
  fields), which `host_impl.retrieve_nix_entity` `isinstance`-discriminates →
  `FxtrID.from_id_string(...)` with no build/copy + a defense-in-depth
  `FxtrID.get_tag()` check (rejects a non-primitive `!`/`.` id smuggled in via
  a hand-written builtin attr); factored the NixError→`NixEntityBuildFailed`
  mapping into `_nix_entity_build_failed`. Added `fxtr.entities.basic_type`
  (hand-written `{type="builtin"; idString="!basic_type";}`, no lib dep so the
  fixture doesn't need a fresh lib commit) to `basic_test_demo`'s flake + a
  retrieve step in `test_in_memory_e2e_retrieve_nix_entity` + `eval_entity_metadata`
  unit tests (layer + builtin) in `test_nix_util.py`. **Passes** (retrieve E2E
  26s; nix_util 10/10; confirmed the bare primitive FxtrID round-trips the
  return bridge with no scope membership). CODE_OVERVIEW updated.
- [x] **Part 2 — `lib.fxtr.entities`.** Done via a **stub package** (cleaner
  than the heavy combined venv the plan first imagined): created
  `nix/fxtr_lib_type_ids/` — re-declares each type id via
  `define_type(SAME_NS, label)`, **fxtr2-only**, content-identical to the
  canonical types (so no `fxtr_workstation_mcp`/`fxtr_report_builder` closure
  under uv2nix). `nix/lib/entities.nix` builds `lib.fxtr.entities.*` from one
  light venv over the stub (+ `basicType` via `builtinEntity`); wired through
  `default.nix`. **No constants added to the library packages** — the stub
  owns the mirrors; `Cls.fxtr_type` / existing `*_TYPE_ID` consts were the
  fallback but the stub supersedes them. Verified: all 8 (NS,label) pairs
  reproduce the baked literals (checked against real fxtr2), and two layers
  built + read back with matching id_strings.
- [x] **`builtinEntity` hardening (per review):** rejects any id not starting
  with `!` or equal to `.` (content ids must use `mkEntityFromPython`).
  Verified accept/reject via `nix eval`.
- [x] (Host-importability moot — the stub depends only on fxtr2; no library
  package is imported.)
- [x] Slow/nix-gated test: `nix/py_testing/tests/test_lib_entities.py` builds
  two `lib.fxtr.entities.*.entity.layer`s and asserts the pinned id_string ==
  the baked literal. **Passes** (3s cached; the lib regression tests
  — entity_from_python / mk_ui_preset / hello_world — still pass too).
- [x] Update CODE_OVERVIEW.md (`nix/CODE_OVERVIEW.md`: `builtinEntity`,
  `entities`, the `fxtr_lib_type_ids` stub package, `test_lib_entities.py`).

## Relevant code

- `nix/lib/default.nix` — the `lib.fxtr` assembly; `workstationExternals`,
  `mkVenv`, `mkEntityFromPython`, `mkUiPreset` live here.
- `nix/lib/mkUiPreset.nix` — unchanged (passes `forType.entity` through).
- `nix/lib/mkEntityFromPython.nix` — the `{ type, layer, layerKey }`
  producer; the builtin shape is its sibling.
- `projects/fxtr_workstation/src/fxtr_workstation/dynamic_dispatch/nix_configurable_dispatch/{nix.py,service_impl.py}`
  — `_PRESET_APPLY` + `set_preset` validation.
- `projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/fxtr_workstation_mcp/{nix_util.py,host_impl.py}`
  — the `retrieve_nix_entity` path (Part 1b).
- `examples/fxtr_workstation_demo/agent_workdir/renderers/builtin/fxtr_renderer.json`
  — the source of the id-string literals + the `!basic_type` key.

## End-of-stage check

- A hand-written preset with a `builtin` `forType` resolves through
  `set_preset` → `resolve` with no layer build, and mis-keying it
  raises `RendererMisconfigured`.
- `lib.fxtr.entities.<name>` evaluates to a `forType`-shaped value;
  building its `entity` yields a layer whose id_string == the baked
  literal.
- (If Part 1b landed) `retrieve_nix_entity` of a `builtin` entity
  returns the primitive `FxtrID`.
- No demo files touched.
