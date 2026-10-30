# SimpleRunResult renderer slice for the demo's `default` UI preset.
# Keyed by the run-result ENTITY's type id
# (simple_tasks.SimpleRunResult.fxtr_type). Base externals suffice.
{ lib, runResultType, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
in
{
  simple_run_result = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."simple_run_result" = {
      forType = runResultType;
      forDispatchKeys = [
        fullPanel
        blockLink
        inlineLink
        hoverPreview
      ];
    };
  };
}
