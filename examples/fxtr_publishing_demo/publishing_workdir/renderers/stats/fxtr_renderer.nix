# Stats renderer slice for the publishing demo's `demo` UI preset. `statsType`
# comes from the root flake. Base externals suffice (workstation-dispatch + core).
{ lib, statsType, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
in
{
  stats = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."stats" = {
      forType = statsType;
      forDispatchKeys = [
        fullPanel
        blockLink
        inlineLink
        hoverPreview
      ];
    };
  };
}
