# Renderer slice for the publishing-serve E2E `demo` preset (the shape
# `lib.mkUiPreset` consumes). `forType` is a **builtin** (a primitive `!`-id), so
# there's no typeEntity layer to build — the dispatcher's builtin branch validates
# the idString directly, keeping the fixture light. The index.tsx imports nothing,
# so the base externals set suffices.
{ lib }:
let
  # `mkUiPreset` wants `forType = { idString; entity; }` — the hand-pasted id
  # plus the type entity `_PRESET_APPLY` inspects. For a builtin both halves come
  # from the same primitive id.
  reportTypeId = "!publishing_demo_report_type";
in
{
  report = {
    renderer = lib.esbuildRenderer { src = ./.; };
    subtypes."report" = {
      forType = {
        idString = reportTypeId;
        entity = lib.builtinEntity { idString = reportTypeId; };
      };
      forDispatchKeys = [ "11111111-1111-1111-1111-111111111111" ];
    };
  };
}
