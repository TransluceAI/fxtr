# Note renderer slice for the demo's `default` UI preset. Receives `noteType`
# (built from the type_entities aggregator venv) from the root flake. Base
# externals suffice (index.tsx imports only base-set @fxtr-ui packages).
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
