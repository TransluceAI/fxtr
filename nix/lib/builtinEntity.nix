# builtinEntity — the non-layer entity attr for a *primitive* FxtrID whose
# id_string IS its own content address (e.g. the `!basic_type` fallback key).
#
# Sibling shape to `mkEntityFromPython`'s `{ type = "fxtr_layer_entity"; layer;
# layerKey; }`: there's nothing to pin in a layered store (the id_string is the
# whole entity), so the attr carries the id_string directly. Consumers special-
# case `type == "builtin"`:
#   - the nix/git dispatcher's preset path validates `idString == <preset key>`
#     with no build (see `_PRESET_APPLY` + `set_preset` in
#     `fxtr_workstation/.../nix_configurable_dispatch/`);
#   - `retrieve_nix_entity` can resolve it straight to a bare `FxtrID`.
#
# Only *primitive* FxtrIDs are valid builtins: the `!`-tag family (e.g.
# `!basic_type`) and the bare `.` (the null/empty id). A content-addressed id
# (`d`/`c`/`n`/… — a real, buildable type entity) must NOT be smuggled in as a
# builtin to skip its validation; those go through `mkEntityFromPython` so the
# dispatcher can build + check them. We reject anything else loudly.
#
# Design (under projects/fxtr_workstation/plans/phase0/part5_nix_workdir/):
#   stage6pre1_builtin_and_lib_entities.md + design_notes/06_ui_presets.md.
{ idString }:
let
  isPrimitive = (builtins.substring 0 1 idString) == "!" || idString == ".";
in
if !isPrimitive then
  throw (
    "lib.fxtr.builtinEntity: idString ${builtins.toJSON idString} is not a primitive "
    + "FxtrID — only the `!`-tag family (e.g. \"!basic_type\") and the bare \".\" are "
    + "builtins. A content-addressed type id has a real buildable type entity; declare "
    + "it via mkEntityFromPython (so the dispatcher builds + validates it), not builtinEntity."
  )
else
  {
    type = "builtin";
    inherit idString;
  }
