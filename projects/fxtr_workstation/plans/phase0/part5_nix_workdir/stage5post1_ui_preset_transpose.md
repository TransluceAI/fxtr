# Stage 5 post-1 — `mkUiPreset` (per-renderer declaration → preset transpose)

A small, self-contained follow-up to Stage 5 (`stage5_ui_presets.md`). Stage 5
shipped the dispatcher consuming an **already-transposed**, hand-written preset
attr (`fxtr.workstation.uiPresets.<name>` keyed by type id_string). This
restores the pattern the pre-nix `demo_dispatch.py` had: each renderer carries
its own co-located "what I support" declaration (the `fxtr_renderer.json`
analog), and a helper **transposes** those into the type-keyed table the
dispatcher already eats — with duplicate-`(type, dispatch_key)` detection.

> _NOTE 2026-06-02: the per-subtype `dispatchKeys` field shown below was later
> renamed to `forDispatchKeys` (to parallel its sibling `forType`); the snippets
> here keep the original name as a record of this stage._

It is **purely additive**: the dispatcher, `eval_preset_table`, and
`_PRESET_APPLY` (`nix_configurable_dispatch/nix.py`) are unchanged — `mkUiPreset`
produces exactly the shape they already consume. The hand-written form keeps
working.

## Background (what got lost, restored)

`examples/fxtr_workstation_demo/demo_dispatch.py` had each renderer subdir carry
an `fxtr_renderer.json` keyed by *subtype label*, each
`{ for_type_id, dispatch_keys: [uuid…] }`. `_build_routing_table` inverted those
into the global `type_id → {uuid → (subtype, subdir)}` table and **raised on a
duplicate `(type, uuid)` pair** (sharding one type across files is allowed as
long as keys don't overlap). Stage 5's preset attr is the *assembled* form, so
that co-located declaration + transpose + conflict-check was lost.
`mkUiPreset` is the nix analog of `_build_routing_table`.

## Decisions (pinned with the user 2026-06-01)

1. **Option X retained.** The type key is a *hand-pasted* id_string (printed
   from Python). Keying the table from eval alone (no builds to learn keys)
   is what keeps a future lazy-renderer-build path open; eager
   key↔`typeEntity` validation in `set_preset` backstops a stale paste.
2. **`forType` bundles `{ idString, entity }`.** The id_string and its
   `typeEntity` (a built `mkEntityFromPython` attr) both identify one type, so
   they travel together as a single value the author defines **once** in a
   `let` and references from every shard. This makes "first-writer-wins" for
   the type-level `typeEntity` a non-issue: shards reference the same value.
3. **Renderer drv, not src.** An entry supplies a prebuilt `renderer`
   (`esbuildRenderer {…}` output). `mkUiPreset` is a pure transpose; it builds
   and imports nothing.
4. **Multiple subtypes per entry.** An entry's `subtypes` is a map (like
   `fxtr_renderer.json`'s top level); the map key *is* the `subtypeKey`. One
   compiled module (`renderer`) backs all of that entry's subtypes, and a
   single entry may declare subtypes for different types.
5. **Entries are a name-keyed attrset, not a list.** Each shard gets a
   user-chosen name — the closest analog to the old subdir name — so the
   conflict `throw` can cite *which* entries collided (no separate label
   field, no nix convention for "name the source" needed).
6. **Errors plumb through unchanged.** A duplicate `(idString, uuid)` is a
   `throw`; it fires when `set_preset`'s `--apply` forces the attr →
   `NixPresetEvalError` → `invoke_error`. No special-casing. Field validation
   is **minimal** — a missing/mistyped field surfaces as nix's own
   attribute-access error.

## Shape

Author side (one type defined once, referenced by every shard):

```nix
let
  demoDataType = { idString = "dlt7…piuhq"; entity = demoDataTypeEntity; };
in
lib.fxtr.mkUiPreset {
  demo_data = {                                  # entry name (collision label)
    renderer = demoDataRenderer;                 # esbuildRenderer output (ESM drv)
    subtypes."demo_data" = {                      # map key = subtypeKey
      forType      = demoDataType;
      dispatchKeys = [ "21b2…" "b3c4…" "c4d5…" "d5e6…" ];
    };
  };
}
```

Transposes to the Stage-5 table verbatim:

```nix
{
  "dlt7…piuhq" = {
    typeEntity = demoDataTypeEntity;             # = forType.entity
    byDispatchKey = {
      "21b2…" = { subtypeKey = "demo_data"; renderer = demoDataRenderer; };
      # … one per uuid
    };
  };
}
```

## `mkUiPreset` semantics (pseudocode)

```
mkUiPreset = entries:
  # Flatten every (entryName, subtypeKey, uuid) row, then fold into the
  # type-keyed table, checking byDispatchKey for the uuid before inserting.
  foldl rows into table where, per row r:
    t = r.forType.idString
    table[t].typeEntity   ?= r.forType.entity          # set once; first wins
    if table[t].byDispatchKey ? r.uuid:
      throw "mkUiPreset: dispatch key <uuid> claimed twice for type <t> —
             by entries '<prevEntry>' and '<r.entryName>'"
    table[t].byDispatchKey[r.uuid] = { subtypeKey = r.subtypeKey;
                                       renderer   = r.renderer; }
```

Pure data fold — `builtins` only (`attrNames` / `concatMap` / `foldl'`); no
`pkgs`, no `system` arg. Lives at `lib.fxtr.mkUiPreset` (a plain function value).

## Implementation steps

1. [x] **`nix/lib/mkUiPreset.nix`** — the transpose above. No factory args beyond
   the entries attrset (it needs nothing from `default.nix`).
2. [x] **`nix/lib/default.nix`** — wire `mkUiPreset = import ./mkUiPreset.nix;` into
   the `lib.fxtr` attrset.
3. [x] **Repo-root `flake.nix` test targets** (under `packages.x86_64-linux`, the
   existing convention for nix-test fixtures) — `ui-preset-transpose-check`
   (verified: shards `type-a` across two entries + multi-subtype `type-b`) +
   `ui-preset-conflict-check` (verified: throws naming both entries):
   - `ui-preset-transpose-check` — `runCommand` writing
     `builtins.toJSON (mkUiPreset {…stubs…})` to `$out`. Stubs are **plain
     attrsets** mimicking the drv/entity shape (`renderer = { drvPath = "/r1"; }`,
     `entity = { type="fxtr_layer_entity"; layer = { drvPath="/e1"; }; layerKey="k1"; }`)
     — `mkUiPreset` passes them through untouched, so `toJSON` serializes them
     with **no build**. Fixture exercises: multiple subtypes in one entry, and
     **sharding** one type across two entries with non-overlapping keys.
   - `ui-preset-conflict-check` — same, but two entries claim the same
     `(idString, uuid)` → `throw` fires when `toJSON` forces the table, so the
     build fails at eval.
4. [x] **`nix/py_testing/tests/test_mk_ui_preset.py`** — uses the existing
   `nix_build` / `flake_ref` fixtures. `test_transpose_inverts_and_shards`
   (structure + passthrough + sharding) + `test_duplicate_dispatch_key_claim_throws`
   (non-zero exit + conflict message in stderr). **2 passed (1.01s).**
5. [x] **Retrofit `examples/basic_test_demo/agent_workdir/flake.nix`** — the
   hand-repeated 4-key block is now a `mkUiPreset` call (one entry, one subtype,
   four keys) + a `let`-bound `demoDataType = { idString; entity; }`. Output is
   identical; the chunk-4 integration test (`test_nix_dispatch_preset.py`) passes
   through the real `mkUiPreset` build path unchanged. **Passed (26.39s).**
6. [x] **Docs** — `design_notes/06_ui_presets.md` "preset assembly via
   `mkUiPreset`" subsection (hand-written form still works);
   `examples/basic_test_demo/agent_workdir/README.md` UI-preset section.
7. [x] **Update `nix/CODE_OVERVIEW.md`** — new `mkUiPreset.nix` helper + the two
   new test targets + `test_mk_ui_preset.py`.

## Out of scope / unchanged

- The dispatcher, `nix_configurable_dispatch/nix.py`, `_PRESET_APPLY`, eager
  validation — all unchanged (`mkUiPreset` output == their existing input).
- The big multi-renderer exercise is the Stage-6 `fxtr_workstation_demo`
  migration (porting its four `fxtr_renderer.json` renderers onto `mkUiPreset`);
  this task lands the helper + a one-entry retrofit + a focused fixture test.
- `dispatchKeys` UUID values stay user-owned literals (the dispatch-key-string
  follow-up in `followups.md` is separate).
```
