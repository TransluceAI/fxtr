# Transcript renderer slice for the demo's `default` UI preset. Receives
# `transcriptType` (built from the type_entities aggregator venv) from the root
# flake. Base externals suffice.
{ lib, transcriptType, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
in
{
  transcript = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."transcript" = {
      forType = transcriptType;
      forDispatchKeys = [
        fullPanel
        blockLink
        inlineLink
        hoverPreview
      ];
    };
  };
}
