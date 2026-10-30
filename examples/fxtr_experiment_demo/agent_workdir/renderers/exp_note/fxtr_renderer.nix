# ExpNote renderer slice for the demo's `default` UI preset. Receives
# `expNoteType` (built from the type_entities aggregator venv) from the root
# flake. Base externals suffice (index.tsx imports only base-set @fxtr-ui
# packages).
{ lib, expNoteType, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
in
{
  exp_note = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."exp_note" = {
      forType = expNoteType;
      forDispatchKeys = [
        fullPanel
        blockLink
        inlineLink
        hoverPreview
      ];
    };
  };
}
