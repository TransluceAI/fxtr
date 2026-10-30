# Docent-proxy renderer slice for the demo's `default` UI preset. One compiled
# renderer backs all three docent result types; the root flake passes each
# type's `forType` (built from the type_entities aggregator venv). Base
# externals suffice.
{
  lib,
  dqlResultType,
  dqlSchemaType,
  metadataFieldsType,
  keyIds,
}:
let
  inherit (keyIds)
    fullPanel
    blockLink
    inlineLink
    hoverPreview
    ;
  shellKeys = [
    fullPanel
    blockLink
    inlineLink
    hoverPreview
  ];
in
{
  docent_fxtr_proxy = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes = {
      dql_result = {
        forType = dqlResultType;
        forDispatchKeys = shellKeys;
      };
      dql_schema = {
        forType = dqlSchemaType;
        forDispatchKeys = shellKeys;
      };
      metadata_fields = {
        forType = metadataFieldsType;
        forDispatchKeys = shellKeys;
      };
    };
  };
}
