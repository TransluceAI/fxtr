# demo_helper renderer slice for the demo's `default` UI preset. Keyed by the
# `WorkstationDemoHelper` SERVICE's `_service_type_id().id_string()` (the host-side
# driver helper, packages/demo_helper). Same slice shape as `renderers/delayed_echo/`
# — the dispatcher routes service refs through the same type-id table as entities.
# Receives `demoHelperType` (built from the type_entities aggregator venv, which
# imports the marker package packages/demo_helper) from the root flake. Base
# externals suffice.
{ lib, demoHelperType, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
in
{
  demo_helper = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."demo_helper" = {
      forType = demoHelperType;
      forDispatchKeys = [
        fullPanel
        blockLink
        inlineLink
        hoverPreview
      ];
    };
  };
}
