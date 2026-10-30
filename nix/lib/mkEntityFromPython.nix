# mkEntityFromPython — materialize a Python-resolved entity object into a
# layered file store, returning the three-field entity attr.
#
# Runs `mk_entity_from_python_writer.py` in the *caller-supplied* venv
# (which provides fxtr2 + the module named by `fromPython`), handing over
# `fromPython` + `layerKey` via __structuredAttrs. The returned `layer`
# derivation pins the resolved entity under `layerKey`. See
# design_notes/03_entity_declarations.md + 10_nix_infrastructure.md.
{
  canonicalSystem,
  pkgsFor,
}:
{
  # A venv derivation (e.g. mkVenv output) bearing fxtr2 + the target module.
  venv,
  # "module:attr" / "module:Class.attr" naming an entity object: a type-id
  # FxtrID, an EntityRef, or an Entity instance.
  fromPython,
  # Pin name for the entity inside the produced layer.
  layerKey ? "entity",
  # Derivation name.
  name ? "fxtr-entity-from-python",
  # Single-arch pin; override to build on a different builder.
  system ? canonicalSystem,
}:
let
  pkgs = pkgsFor system;
  layer = pkgs.runCommand name {
    __structuredAttrs = true;
    inherit fromPython layerKey;
  } ''
    ${venv}/bin/python ${./mk_entity_from_python_writer.py}
  '';
in
{
  type = "fxtr_layer_entity";
  inherit layer layerKey;
}
