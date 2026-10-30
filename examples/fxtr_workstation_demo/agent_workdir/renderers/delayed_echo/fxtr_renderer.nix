# delayed_echo renderer slice for the demo's `default` UI preset. Keyed by the
# `delayed_echo` SERVICE's `_service_type_id().id_string()` — the dispatcher
# routes service refs through the same type-id table as entities, so the slice
# shape is identical to the entity renderers'. Receives `delayedEchoType`
# (built from the type_entities aggregator venv, which imports the
# host-importable half of `packages/delayed_echo/`) from the root flake. Base
# externals suffice.
{ lib, delayedEchoType, keyIds }:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
in
{
  delayed_echo = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."delayed_echo" = {
      forType = delayedEchoType;
      forDispatchKeys = [
        fullPanel
        blockLink
        inlineLink
        hoverPreview
      ];
    };
  };
}
