# Note renderer slice for the publishing demo's `demo` UI preset. `noteType` (the
# `{ idString; entity }` built from the builder venv via mkEntityFromPython) comes
# from the root flake. The index.tsx imports only base + workstation-dispatch
# packages, so the default externals set suffices.
{ lib, noteType, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
in
{
  note = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."note" = {
      forType = noteType;
      forDispatchKeys = [
        fullPanel
        blockLink
        inlineLink
        hoverPreview
      ];
    };
  };
}
