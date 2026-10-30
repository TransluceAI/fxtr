# mkUiPreset — transpose per-renderer declarations into the type-keyed preset
# table the nix/git dispatcher consumes.
#
# This is the nix analog of the pre-nix `demo_dispatch.py:_build_routing_table`
# (`examples/fxtr_workstation_demo/`): each renderer carries its own co-located
# "what I support" declaration; this helper inverts those into the global
# `{ "<type-id-string>" = { typeEntity; byDispatchKey = { "<uuid>" = …; }; }; }`
# table — the SAME shape a hand-written `fxtr.uiPresets.<name>`
# attr has (so `eval_preset_table` / `_PRESET_APPLY` in
# `fxtr_workstation/.../nix_configurable_dispatch/nix.py` are unchanged), and
# `throw`s on a duplicate `(type-id, dispatch-key)` claim. Sharding one type
# across entries is fine as long as the dispatch keys don't overlap.
#
# Pure structural transpose — builds and imports nothing. The `renderer` (an
# `esbuildRenderer` output) and the `forType.entity` (an `mkEntityFromPython`
# attr) are passed through untouched; the dispatcher builds them later.
#
# Design + decisions:
#   projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage5post1_ui_preset_transpose.md
#   projects/fxtr_workstation/plans/phase0/part5_nix_workdir/design_notes/06_ui_presets.md
#
# Shape of `entries` (a name-keyed attrset; the entry name labels collisions):
#
#   {
#     <entryName> = {
#       renderer = <esbuildRenderer output>;        # one ESM drv per entry
#       subtypes = {
#         <subtypeKey> = {                            # map key = subtypeKey
#           forType         = { idString = "<id>"; entity = <mkEntityFromPython>; };
#           forDispatchKeys = [ "<uuid>" … ];
#         };
#       };
#     };
#   }
#
# `forType` bundles the hand-pasted id_string with its type entity so an author
# defines a type once (in a `let`) and references it from every shard; the
# type-level `typeEntity` is set by the first row that names the type (shards
# sharing one `forType` value are identical, so first-wins is a non-issue).
entries:
let
  # Flatten every (entryName, subtypeKey, uuid) declaration into a flat row
  # list, carrying the renderer + resolved type along for the fold.
  rows = builtins.concatMap (
    entryName:
    let
      entry = entries.${entryName};
    in
    builtins.concatMap (
      subtypeKey:
      let
        sub = entry.subtypes.${subtypeKey};
      in
      map (uuid: {
        inherit entryName subtypeKey uuid;
        idString = sub.forType.idString;
        entity = sub.forType.entity;
        renderer = entry.renderer;
      }) sub.forDispatchKeys
    ) (builtins.attrNames entry.subtypes)
  ) (builtins.attrNames entries);

  # Fold rows into the type-keyed table; `seen` maps "<idString>\t<uuid>" to the
  # entry that first claimed it, for the duplicate-claim throw message.
  step =
    acc: row:
    let
      t = row.idString;
      seenKey = "${t}\t${row.uuid}";
    in
    if acc.seen ? ${seenKey} then
      throw (
        "mkUiPreset: dispatch key '${row.uuid}' is claimed twice for type "
        + "'${t}' — by entry '${acc.seen.${seenKey}}' and entry "
        + "'${row.entryName}'"
      )
    else
      let
        existing = acc.table.${t} or {
          typeEntity = row.entity;
          byDispatchKey = { };
        };
      in
      {
        seen = acc.seen // { ${seenKey} = row.entryName; };
        table = acc.table // {
          ${t} = existing // {
            byDispatchKey = existing.byDispatchKey // {
              ${row.uuid} = {
                subtypeKey = row.subtypeKey;
                renderer = row.renderer;
              };
            };
          };
        };
      };
in
(builtins.foldl' step {
  table = { };
  seen = { };
} rows).table
