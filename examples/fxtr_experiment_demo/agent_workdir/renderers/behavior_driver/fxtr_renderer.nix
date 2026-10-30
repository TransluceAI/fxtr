# Stage-picker renderer slice for the `behavior-demo-helper` SERVICE
# (host-side driver defined in
# examples/fxtr_experiment_demo/behavior/behavior_demo_helper_service.py).
# Receives `behaviorDriverType` from the root flake. Base externals suffice.
{ lib, behaviorDriverType, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
in
{
  behavior_driver = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."behavior_driver" = {
      forType = behaviorDriverType;
      forDispatchKeys = [
        fullPanel
        blockLink
        inlineLink
        hoverPreview
      ];
    };
  };
}
