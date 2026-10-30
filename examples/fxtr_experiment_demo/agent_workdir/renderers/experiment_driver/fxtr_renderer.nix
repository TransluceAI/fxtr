# Scenario-picker renderer slice for the `experiment-demo-helper` SERVICE
# (host-side driver defined in
# examples/fxtr_experiment_demo/experiment_demo_helper_service.py). Receives
# `experimentDriverType` from the root flake. Base externals suffice.
{ lib, experimentDriverType, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
in
{
  experiment_driver = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."experiment_driver" = {
      forType = experimentDriverType;
      forDispatchKeys = [
        fullPanel
        blockLink
        inlineLink
        hoverPreview
      ];
    };
  };
}
